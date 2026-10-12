"""Contract tests for scripts/lighthouse_a11y_contrast_scan.py.

The scanner is the deps-free, infra-free first pass for /lighthouse/* WCAG
contrast. These tests pin the math (so a future refactor can't silently
flip a pass into a fail) and assert the known-bad pairings stay flagged
until the design system addresses them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lighthouse_a11y_contrast_scan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("lighthouse_a11y_contrast_scan", _SCRIPT)
    assert spec and spec.loader, "scanner script not loadable"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lighthouse_a11y_contrast_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scanner():
    return _load_module()


class TestContrastRatio:
    def test_black_on_white_is_21(self, scanner):
        r = scanner.contrast_ratio((0, 0, 0), (255, 255, 255))
        assert round(r, 2) == 21.00

    def test_white_on_white_is_1(self, scanner):
        r = scanner.contrast_ratio((255, 255, 255), (255, 255, 255))
        assert round(r, 2) == 1.00

    def test_ink_brown_on_oat_passes_aa(self, scanner):
        r = scanner.contrast_ratio(scanner.INK_BROWN, scanner.OAT)
        assert r >= 4.5, f"primary body text must pass WCAG AA, got {r:.2f}:1"


class TestKnownFailures:
    """These pairings are intentionally pinned as failing.

    If a future redesign improves the palette, flip the assertion (the test
    will tell you which pairing was fixed and force a doc update).
    """

    @pytest.mark.parametrize(
        "fg_name,bg_name,wcag",
        [
            ("clay", "oat", "AA-large"),        # 2.07:1
            ("clay-deep", "oat", "AA-large"),   # 2.80:1
            ("sage", "oat", "AA-large"),        # 2.76:1
            ("dusk-rose", "oat", "AA-large"),   # 1.95:1
            ("white", "lantern-deep", "AA-normal"),       # 1.71:1
        ],
    )
    def test_pairing_is_failing(self, scanner, fg_name, bg_name, wcag):
        pairings = scanner._build_pairings()
        match = [p for p in pairings if p.fg_name == fg_name and p.bg_name == bg_name and p.wcag_target == wcag]
        assert match, f"pairing {fg_name}-on-{bg_name} ({wcag}) missing from scanner"
        assert not match[0].passes(), (
            f"pairing {fg_name}-on-{bg_name} now passes ({match[0].ratio():.2f}:1); "
            "flip the test and update docs/audits/lighthouse-a11y-2026-05-27.md"
        )


class TestKnownPasses:
    @pytest.mark.parametrize(
        "fg_name,bg_name",
        [
            ("ink-brown", "oat"),
            # ink-faint + ink-soft tokens raised to alpha 0.85 in the
            # /lighthouse/* views (auto/harbor-a11y-ink-faint-on-oat).
            # Both now composite to 6.59:1 on oat and clear WCAG AA.
            ("ink-faint", "oat"),
            ("ink-soft", "oat"),
            ("ink-0.85", "oat"),
            ("ink-brown", "lantern-amber"),
            ("ink-1a", "cream-f6"),
            ("ink-5a", "cream-f6"),
            ("ink-5e", "cream-fffaf2"),
            ("ink-6e", "cream-fffaf2"),
            ("slate-blue", "oat"),
            ("ink-brown", "lantern-deep"),
            ("copper-9a5a42", "cream-fdf2e8"),
        ],
    )
    def test_pairing_passes(self, scanner, fg_name, bg_name):
        pairings = scanner._build_pairings()
        match = [p for p in pairings if p.fg_name == fg_name and p.bg_name == bg_name]
        assert match, f"pairing {fg_name}-on-{bg_name} missing"
        assert match[0].passes(), f"regression: {fg_name}-on-{bg_name} dropped to {match[0].ratio():.2f}:1"


class TestRenderMarkdown:
    def test_emits_table_header(self, scanner):
        md = scanner.render_markdown(scanner._build_pairings())
        assert "| FG | BG | Use case | Ratio | Target | Status | Routes |" in md
        assert "**Pairings scanned:**" in md
        assert "**Failing:**" in md

    def test_pairing_count_matches_build(self, scanner):
        md = scanner.render_markdown(scanner._build_pairings())
        n = len(scanner._build_pairings())
        assert f"**Pairings scanned:** {n}" in md
