"""Section-05: layout contract tests (§4.5)."""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from cli.contracts import ContractViolation  # noqa: E402
from cli.contracts import layout as _layout  # noqa: E402


def _artifact_with(layout_ejs: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        spec={},
        files=[("views/layout.ejs", layout_ejs)],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/test",
    )


def test_layout_fails_on_em_dash_in_brand_nav():
    """layout.check() fails on U+2014 em-dash in brand nav text."""
    # U+2014 is "\u2014"; appended to a word-mark brand.
    bad = (
        "<html><body>\n"
        '<nav><span class="brand">Medicine \u2014 Waves</span></nav>\n'
        "<main>x</main>\n"
        "</body></html>\n"
    )
    art = _artifact_with(bad)
    with pytest.raises(ContractViolation) as exc_info:
        _layout.check(art, collect_all=False)
    assert "em-dash" in str(exc_info.value).lower() or "U+2014" in str(exc_info.value)


def test_layout_passes_with_en_dash_outside_brand_nav():
    """layout.check() passes on U+2013 en-dash in number-range content outside brand nav.

    En dash (U+2013), figure dash (U+2012), horizontal bar (U+2015) are
    permitted everywhere; em dash (U+2014) is the only banned character.
    """
    good = (
        "<html><body>\n"
        '<nav><span class="brand">Medicine Waves</span></nav>\n'
        "<main>Open hours: 9\u201312 \u2012 Mon\u2013Fri \u2015 always on</main>\n"
        "</body></html>\n"
    )
    art = _artifact_with(good)
    _layout.check(art, collect_all=False)


def test_layout_fails_when_main_missing():
    """layout.check() fails when <main> landmark missing (a11y)."""
    bad = (
        "<html><body>\n"
        '<nav><span class="brand">Brand</span></nav>\n'
        "<div>no main here</div>\n"
        "</body></html>\n"
    )
    art = _artifact_with(bad)
    with pytest.raises(ContractViolation):
        _layout.check(art, collect_all=False)


def test_layout_fails_when_nav_brand_missing():
    """layout.check() fails when <nav> brand element missing."""
    bad = (
        "<html><body>\n"
        "<nav></nav>\n"
        "<main>x</main>\n"
        "</body></html>\n"
    )
    art = _artifact_with(bad)
    with pytest.raises(ContractViolation) as exc_info:
        _layout.check(art, collect_all=False)
    assert "brand" in str(exc_info.value).lower()


def test_layout_passes_on_canonical_mw_layout():
    """layout.check() passes on canonical MW layout.ejs."""
    canonical = (
        "<html><body>\n"
        '<nav class="site-nav">\n'
        '<a href="/" class="brand"><img src="/logo.png" alt="Medicine Waves"></a>\n'
        "</nav>\n"
        '<main id="main-content">x</main>\n'
        "</body></html>\n"
    )
    art = _artifact_with(canonical)
    _layout.check(art, collect_all=False)
