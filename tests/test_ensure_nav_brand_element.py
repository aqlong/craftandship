"""Regression coverage for cli.build._ensure_nav_brand_element.

Background: contracts/layout.py requires the first <nav> in
views/layout.ejs to contain a brand element (logo img or word-mark).
Before this post-processor, non-MW customer builds whose Claude-emitted
nav had only text links (no img / .brand / .logo / h1 / span.site-name)
failed the contract with "<nav> missing brand element" and never
reached deploy.

The post-processor is idempotent and surgical: when any brand marker
is already present it is a no-op; otherwise a text wordmark is injected
as the first child of the first <nav>.
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)


def _run(layout_ejs: str, spec: dict | None = None) -> str:
    """Run the post-processor and return the layout.ejs string it wrote."""
    import build
    files = [("views/layout.ejs", layout_ejs)]
    result = dict(build._ensure_nav_brand_element(files, spec or {}))
    return result["views/layout.ejs"]


# ---------------------------------------------------------------------------
# Brand-absent: wordmark should be injected
# ---------------------------------------------------------------------------


def test_injects_wordmark_when_nav_has_only_text_links():
    """Claude's common failure mode: nav contains only anchor links with
    text like "Home" / "About" / "Contact" and no brand marker. Contract
    fires. Post-processor must inject a brand wordmark."""
    layout = (
        "<!DOCTYPE html>\n<html><body>\n"
        '<nav class="flex gap-4 p-4">\n'
        '  <a href="/">Home</a>\n'
        '  <a href="/about">About</a>\n'
        '  <a href="/contact">Contact</a>\n'
        "</nav>\n"
        "<main></main>\n"
        "</body></html>"
    )
    spec = {"app_name": "sample-practitioner", "settings": {"site_name": "Sample Practitioner"}}
    out = _run(layout, spec)
    # Brand marker present
    assert 'class="brand' in out or "class='brand" in out, (
        f"expected brand class injected; got:\n{out}"
    )
    # Specific site_name used
    assert "Sample Practitioner" in out, f"expected site_name in wordmark; got:\n{out}"
    # Injected inside first <nav>, BEFORE the Home anchor
    nav_start = out.find("<nav")
    first_home_anchor = out.find('href="/"')
    assert nav_start < first_home_anchor, "wordmark must be first child of nav"


def test_injected_wordmark_passes_layout_contract():
    """End-to-end: feed a brand-less nav into the post-processor and run
    contracts/layout.check() on the output. Zero violations expected."""
    import types
    from cli.contracts import layout as _layout_contract
    layout_ejs = (
        '<!DOCTYPE html><html><body>\n'
        '<nav><a href="/about">About</a></nav>\n'
        '<main></main>\n'
        '</body></html>'
    )
    out = _run(layout_ejs, {"app_name": "test-app"})
    artifact = types.SimpleNamespace(files=[("views/layout.ejs", out)])
    violations: list[str] = []
    _layout_contract.check(artifact, collect_all=True, violations=violations)
    # layout contract covers more than just brand (nav + main + em-dash).
    # Our input already has <main>; so filter out anything unrelated.
    brand_violations = [v for v in violations if "brand" in v.lower()]
    assert not brand_violations, (
        f"brand contract must pass after injection; got: {brand_violations}"
    )


def test_wordmark_prefers_site_name_then_business_name_then_app_name():
    base_layout = "<nav><a href='/x'>X</a></nav>"
    # site_name wins
    out = _run(
        base_layout,
        {
            "app_name": "aaa",
            "settings": {"site_name": "Site", "business_name": "Biz"},
        },
    )
    assert ">Site<" in out
    # business_name when site_name absent
    out = _run(
        base_layout,
        {"app_name": "aaa", "settings": {"business_name": "Biz"}},
    )
    assert ">Biz<" in out
    # prettified app_name when settings absent
    out = _run(base_layout, {"app_name": "small-business-invoicer"})
    assert ">Small Business Invoicer<" in out
    # "-v<N>-<N>" version suffix is stripped
    out = _run(base_layout, {"app_name": "sample-practitioner-v1-123456"})
    assert ">Sample Practitioner<" in out
    # fallback "Home" when nothing available
    out = _run(base_layout, {})
    assert ">Home<" in out


def test_wordmark_html_escapes_to_prevent_attribute_break():
    """If app_name or settings values contain HTML-special characters
    they must be escaped to prevent breaking the anchor attribute."""
    out = _run(
        "<nav><a href='/x'>X</a></nav>",
        {"settings": {"site_name": 'Evil" <script>'}},
    )
    # Raw < must NOT appear unescaped in wordmark; & / &lt; accepted.
    assert "<script>" not in out, "wordmark must escape HTML characters"
    assert "&quot;" in out or "&#x27;" in out or "&lt;" in out, (
        "expected HTML entities after escaping; got:\n" + out
    )


# ---------------------------------------------------------------------------
# Brand-present: idempotent no-op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "existing_nav",
    [
        # img in nav
        '<nav><a href="/"><img src="/logo.png" alt="logo"></a></nav>',
        # class="brand"
        '<nav><a href="/" class="brand">X</a></nav>',
        # class="logo"
        '<nav><span class="logo">X</span></nav>',
        # class="wordmark"
        '<nav><span class="wordmark">Sample Practitioner</span></nav>',
        # class="site-name"
        '<nav><span class="site-name">Name</span></nav>',
        # <h1> inside nav
        '<nav><h1>Site Title</h1></nav>',
        # Pre-existing brand in multi-class attribute
        '<nav><a class="flex items-center brand gap-2" href="/">Brand</a></nav>',
    ],
)
def test_noop_when_brand_already_present(existing_nav):
    """Post-processor must not touch a nav that already satisfies the
    contract. Locks in idempotency and prevents double-injection on
    reapply passes."""
    layout = f"<!DOCTYPE html><html><body>{existing_nav}<main></main></body></html>"
    out = _run(layout, {"app_name": "test-app"})
    assert out == layout, (
        f"expected no-op; input:\n{layout}\n\noutput:\n{out}"
    )


def test_noop_idempotent_across_repeat_runs():
    """Running the post-processor twice must produce the same output as
    running once. Critical for reapply passes (same spec, same files)."""
    layout = (
        "<!DOCTYPE html><html><body>\n"
        '<nav><a href="/">Home</a></nav>\n'
        "<main></main>\n"
        "</body></html>"
    )
    first = _run(layout, {"app_name": "test"})
    second = _run(first, {"app_name": "test"})
    assert first == second, "second run changed output; not idempotent"


# ---------------------------------------------------------------------------
# Defensive edges
# ---------------------------------------------------------------------------


def test_skips_when_layout_ejs_missing():
    """No layout.ejs in files -> no-op, no crash."""
    import build
    files = [("views/home.ejs", "<h1>Home</h1>")]
    out = dict(build._ensure_nav_brand_element(files, {}))
    assert out == {"views/home.ejs": "<h1>Home</h1>"}


def test_skips_when_no_nav_in_layout():
    """Layout without any <nav> -> no-op; contract is not our concern
    here (the contract will still fail on 'nav' selector but that is a
    separate contract failure and this post-processor deliberately does
    not try to fabricate an entire nav element from nothing)."""
    layout = "<!DOCTYPE html><html><body><main></main></body></html>"
    out = _run(layout, {"app_name": "test"})
    assert out == layout


def test_handles_nav_with_attributes():
    """Regression guard: <nav class="..." aria-label="..."> must parse
    correctly and the brand should be injected immediately after the
    opening tag."""
    layout = (
        '<nav class="flex gap-4" aria-label="Primary" data-x="y">\n'
        '  <a href="/about">About</a>\n'
        "</nav>"
    )
    out = _run(layout, {"app_name": "test"})
    nav_open_close = out.find(">", out.find("<nav"))
    inject_pos = out.find("<a", nav_open_close)
    # First <a> after the nav opening should be the injected brand anchor
    # (href="/"), not the pre-existing About link.
    assert 'href="/"' in out[nav_open_close:inject_pos + 200], (
        f"brand anchor must land immediately after opening <nav>; got:\n{out}"
    )
