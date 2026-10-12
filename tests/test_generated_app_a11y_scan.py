"""Contract tests for scripts/generated_app_a11y_scan.py.

The scanner is the deps-free, infra-free first pass for v2 compose
generated-app a11y. These tests pin the static-scan logic AND the
known-bad pairings so a future change cannot silently flip a fail
into a pass without updating
`docs/audits/compose-archetype-a11y-2026-05-27.md` too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "generated_app_a11y_scan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("generated_app_a11y_scan", _SCRIPT)
    assert spec and spec.loader, "scanner script not loadable"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["generated_app_a11y_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scanner():
    return _load_module()


class TestContrastMath:
    def test_black_on_white_is_21(self, scanner):
        assert round(scanner.contrast_ratio((0, 0, 0), (255, 255, 255)), 2) == 21.00

    def test_white_on_white_is_1(self, scanner):
        assert round(scanner.contrast_ratio((255, 255, 255), (255, 255, 255)), 2) == 1.00

    def test_hex_to_rgb_round_trip(self, scanner):
        assert scanner._hex_to_rgb("#3a2f24") == (58, 47, 36)
        assert scanner._hex_to_rgb("#fff") == (255, 255, 255)


class TestScanFile:
    def test_image_with_alt_passes(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            '<img src="/x.jpg" alt="A cat" />',
        )
        rules = {f["rule"] for f in findings}
        assert "missing-alt-on-img" not in rules

    def test_image_with_ejs_in_attrs_does_not_falsely_flag_alt(self, scanner):
        """Regression: EJS tags inside `<img>` attrs contain `>` which prematurely
        terminated the regex match before the EJS-strip pass landed. Without the
        strip, this img, which DOES carry alt=, was getting flagged as
        missing-alt-on-img. Pin the EJS-strip behavior here.
        """
        findings = scanner.scan_file(
            "views/widgets/hero/centered.ejs",
            (
                '<img loading="eager" fetchpriority="high" '
                'src="<%= _imgUrl %>" alt="<%= _imgAlt %>" class="w-full" />'
            ),
        )
        rules = {f["rule"] for f in findings}
        assert "missing-alt-on-img" not in rules

    def test_image_without_alt_is_flagged(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            '<img src="/x.jpg" />',
        )
        rules = {f["rule"] for f in findings}
        assert "missing-alt-on-img" in rules

    def test_input_with_aria_label_passes(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            '<form><input type="email" aria-label="Email"></form>',
        )
        rules = {f["rule"] for f in findings}
        assert "input-missing-label" not in rules

    def test_input_with_associated_label_passes(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            '<form><label for="e">Email</label><input type="email" id="e"></form>',
        )
        rules = {f["rule"] for f in findings}
        assert "input-missing-label" not in rules

    def test_input_with_only_placeholder_is_flagged(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            '<form><input type="email" placeholder="Your email"></form>',
        )
        rules = {f["rule"] for f in findings}
        assert "input-missing-label" in rules

    def test_hidden_input_is_not_flagged(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            '<input type="hidden" name="_csrf" value="x">',
        )
        rules = {f["rule"] for f in findings}
        assert "input-missing-label" not in rules

    def test_button_with_text_passes(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            "<button>Submit</button>",
        )
        rules = {f["rule"] for f in findings}
        assert "button-empty-content" not in rules

    def test_empty_button_is_flagged(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            "<button></button>",
        )
        rules = {f["rule"] for f in findings}
        assert "button-empty-content" in rules

    def test_generic_link_text_is_flagged(self, scanner):
        findings = scanner.scan_file(
            "views/widgets/foo.ejs",
            '<a href="/x">Click here</a>',
        )
        rules = {f["rule"] for f in findings}
        assert "link-generic-text" in rules

    def test_layout_with_main_and_skip_link_passes(self, scanner):
        findings = scanner.scan_file(
            "views/layout-public.ejs",
            """<html lang="en"><body>
            <%- include('./widgets/shell/skip-to-content') %>
            <main id="main-content"><%- body %></main>
            </body></html>""",
        )
        rules = {f["rule"] for f in findings}
        assert "missing-main-landmark" not in rules
        assert "missing-skip-link" not in rules
        assert "missing-html-lang" not in rules

    def test_html_without_lang_is_flagged(self, scanner):
        findings = scanner.scan_file(
            "views/layout-public.ejs",
            "<html><body><main>x</main></body></html>",
        )
        rules = {f["rule"] for f in findings}
        assert "missing-html-lang" in rules


class TestPaletteContrast:
    def test_palette_scan_returns_known_failures(self, scanner):
        """Pin both palette-muted fixes listed in the audit doc.
        `sanctuary` was darkened from `#6e7d76` to `#5a665f` (4.93:1) under
        roadmap item `v2-a11y-palette-sanctuary-muted-darken` (2026-05-28),
        clearing AA-normal. `stone-neutral` was darkened from `#7a7a78` to
        `#6a6a6a` (5.09:1) under roadmap item
        `v2-a11y-palette-stone-neutral-muted-darken` (2026-05-28), also
        clearing AA-normal. If either palette regresses, this test will
        fail with a clear diagnostic.
        """
        findings = scanner.scan_palettes_for_low_contrast()
        palette_pairs = {(f["palette"], f["rule"]) for f in findings}
        assert ("sanctuary", "palette-foreground-muted-fails-aa") not in palette_pairs, (
            "sanctuary regressed: --color-foreground-muted must clear 4.5:1 on "
            "--color-background. Fix was #6e7d76 -> #5a665f (4.93:1). If a "
            "later edit lightened it back below AA-normal, restore a darker "
            "value and update docs/audits/compose-archetype-a11y-2026-05-27.md."
        )
        assert ("stone-neutral", "palette-foreground-muted-fails-aa") not in palette_pairs, (
            "stone-neutral regressed: --color-foreground-muted must clear 4.5:1 on "
            "--color-background. Fix was #7a7a78 -> #6a6a6a (5.09:1). If a "
            "later edit lightened it back below AA-normal, restore a darker "
            "value and update docs/audits/compose-archetype-a11y-2026-05-27.md."
        )

    def test_palette_primary_foreground_all_pass_aa(self, scanner):
        """Every palette's --color-foreground on --color-background must pass
        AA-normal. Pinned here so a new palette that ships failing primaries
        cannot land silently.
        """
        findings = scanner.scan_palettes_for_low_contrast()
        primary_fails = [f for f in findings if f["rule"] == "palette-foreground-fails-aa"]
        assert primary_fails == [], (
            "New palette with --color-foreground failing AA-normal: "
            + ", ".join(f"{f['palette']} ({f['ratio']}:1)" for f in primary_fails)
        )


class TestNineArchetypesCovered:
    def test_archetype_list_is_nine(self, scanner):
        assert len(scanner.ARCHETYPES) == 9
        # Stable order so the audit doc table stays diffable.
        assert scanner.ARCHETYPES[0] == "healer-wellness"
        assert scanner.ARCHETYPES[-1] == "event-producer"


class TestNewsletterWidgetHasAccessibleName:
    """Pins the v2-a11y-newsletter-widget-aria-label fix (2026-05-28). The
    newsletter inline widget previously emitted an unlabeled <input type=
    "email">, which axe-core / WCAG 1.3.1 + 3.3.2 flag (placeholder is not
    an accessible name and is stripped by some assistive tech once the
    user starts typing). The fix added an aria-label AND a sibling sr-only
    <label for=...> so future visible-label promotion is a class swap.
    A future widget edit that removes either accessible-name affordance
    will fail this test with a clear diagnostic.
    """

    def test_newsletter_inline_widget_does_not_emit_input_missing_label(self, scanner):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        widget_path = repo_root / "compose" / "widgets" / "cta" / "newsletter-inline.ejs"
        content = widget_path.read_text()
        findings = scanner.scan_file(
            "compose/widgets/cta/newsletter-inline.ejs",
            content,
        )
        rules = {f["rule"] for f in findings}
        assert "input-missing-label" not in rules, (
            "newsletter-inline.ejs regressed input-missing-label. The fix from "
            "roadmap item `v2-a11y-newsletter-widget-aria-label` adds both "
            "aria-label='Email address' AND a sibling <label for=... "
            "class='sr-only'>. If either was removed, restore one (or both) "
            "and update docs/audits/compose-archetype-a11y-2026-05-27.md."
        )
