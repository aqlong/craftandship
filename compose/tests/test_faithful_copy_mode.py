"""Faithful-copy mode (v2-faithful-site-copy-mode) compose tests.

Covers:
  - intake.scaffold_spec accepts _harbor_meta.copy_pages and overrides
    matching pages in spec.pages with hero + _copy_blocks.
  - build.resolve_spec swaps copy-faithful-default layout in for any
    page carrying _copy_blocks and pre-renders the markdown into the
    `copy` block.
  - emit_page_view honors the new layout and includes the
    content-raw-markdown widget.
  - hero-centered widget renders body_html, credential_lines, and
    cta_text_me_phone (sms link).
  - Em dashes in customer-imported copy survive through the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compose.build import BuildSpec, compose_files, resolve_spec, validate_spec
from compose.intake import scaffold_spec


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _base_answers(**overrides) -> dict:
    answers = {
        "app_name": "sample-trades-test",
        "archetype": "tradesperson-contractor",
        "site_name": "Sample Trades Co",
        "admin_email": "owner@example.com",
        "contact_phone": "(555) 555-0112",
    }
    answers.update(overrides)
    return answers


# ---------------------------------------------------------------------------
# scaffold_spec: copy_pages override
# ---------------------------------------------------------------------------

def test_scaffold_spec_overrides_home_with_copy_pages():
    """A copy_pages entry for `/` replaces the archetype home block
    with hero + _copy_blocks + _copy_source_url."""
    answers = _base_answers(_harbor_meta={
        "copy_source_url": "https://sample-trades.example/",
        "copy_pages": [
            {
                "path": "/",
                "markdown_blocks": [
                    "# Sample Trades Co",
                    "A sample paragraph of copy.",
                    "- Sample service one",
                    "- Sample service two",
                ],
                "hero": {
                    "heading": "Sample Trades Co",
                    "credential_lines": ["Sample credential one", "Sample credential two"],
                    "cta_text_me_phone": "5555550112",
                },
            }
        ],
    })
    spec = scaffold_spec(answers)
    home = spec["pages"]["home"]
    assert home["hero"]["heading"] == "Sample Trades Co"
    assert home["hero"]["credential_lines"] == ["Sample credential one", "Sample credential two"]
    assert home["hero"]["cta_text_me_phone"] == "5555550112"
    # Faithful-copy mode rewrites cta_primary to an sms: link.
    assert home["hero"]["cta_primary"]["href"] == "sms:5555550112"
    # The archetype home's offerings/testimonials/etc. blocks are gone.
    # Only hero + _copy_blocks + _copy_source_url remain.
    assert "_copy_blocks" in home
    assert "A sample paragraph of copy." in home["_copy_blocks"]
    assert home["_copy_source_url"] == "https://sample-trades.example/"
    assert "offerings" not in home
    assert "testimonials" not in home
    assert "trust" not in home


def test_scaffold_spec_overrides_about_page():
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/about",
                "markdown_blocks": ["# About", "Sample about copy."],
            }
        ]
    })
    spec = scaffold_spec(answers)
    # Tradesperson archetype includes 'about' in default_pages.
    assert "about" in spec["pages"]
    assert "_copy_blocks" in spec["pages"]["about"]
    assert "Sample about copy." in spec["pages"]["about"]["_copy_blocks"]


def test_scaffold_spec_creates_non_archetype_pages():
    """A copy path that doesn't map to any archetype default still
    lands as a page in spec.pages so the build stage can route it."""
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/work-with-me",
                "markdown_blocks": ["# Work With Me", "Custom intent."],
            }
        ]
    })
    spec = scaffold_spec(answers)
    assert "work-with-me" in spec["pages"]
    assert spec["pages"]["work-with-me"]["_copy_blocks"]


def test_scaffold_spec_empty_markdown_blocks_skips_entry():
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {"path": "/", "markdown_blocks": []},
            {"path": "/about", "markdown_blocks": ["# About", "Real."]},
        ]
    })
    spec = scaffold_spec(answers)
    # Home retains archetype defaults (no override).
    assert "_copy_blocks" not in spec["pages"]["home"]
    # About got overridden.
    assert "_copy_blocks" in spec["pages"]["about"]


def test_scaffold_spec_warns_on_copy_pages_page_id_collision():
    """When two copy_pages entries map to the same compose page_id
    (e.g. '/services/coaching' and '/services-coaching' both collapse
    to 'services-coaching'), the first occurrence wins, the later
    duplicate is dropped, and a translation warning is surfaced so SE
    reviewers can see what happened."""
    answers = _base_answers(_harbor_meta={
        "copy_source_url": "https://example.com/",
        "copy_pages": [
            {
                "path": "/services/coaching",
                "markdown_blocks": ["# Coaching", "First entry copy."],
            },
            {
                "path": "/services-coaching",
                "markdown_blocks": ["# Coaching (alt)", "Second entry copy."],
            },
        ],
    })
    spec = scaffold_spec(answers)
    # First entry wins.
    assert "services-coaching" in spec["pages"]
    blocks = spec["pages"]["services-coaching"]["_copy_blocks"]
    assert "First entry copy." in blocks
    assert "Second entry copy." not in blocks
    # Warning surfaced on the spec.
    warnings = spec.get("_translation_warnings") or []
    collision = [
        w for w in warnings
        if isinstance(w, dict)
        and w.get("reason") == "copy_pages_page_id_collision"
    ]
    assert len(collision) == 1, warnings
    detail = collision[0]["detail"]
    assert "services-coaching" in detail
    assert "/services/coaching" in detail
    assert "/services-coaching" in detail


def test_scaffold_spec_no_collision_warning_when_page_ids_unique():
    """Regression guard: distinct paths must not trigger a collision
    warning."""
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {"path": "/", "markdown_blocks": ["# Home", "Body."]},
            {"path": "/about", "markdown_blocks": ["# About", "Body."]},
        ]
    })
    spec = scaffold_spec(answers)
    warnings = spec.get("_translation_warnings") or []
    collision = [
        w for w in warnings
        if isinstance(w, dict)
        and w.get("reason") == "copy_pages_page_id_collision"
    ]
    assert collision == []


def test_scaffold_spec_collision_warning_merges_with_existing_warnings():
    """When the translator already populated _translation_warnings,
    the collision entries append rather than clobber."""
    pre_existing = [
        {"reason": "invalid_data_model", "detail": "non-dict data_model dropped: str"},
    ]
    answers = _base_answers(
        _translation_warnings=list(pre_existing),
        _harbor_meta={
            "copy_pages": [
                {
                    "path": "/services/coaching",
                    "markdown_blocks": ["# A", "Body."],
                },
                {
                    "path": "/services-coaching",
                    "markdown_blocks": ["# B", "Body."],
                },
            ],
        },
    )
    spec = scaffold_spec(answers)
    warnings = spec.get("_translation_warnings") or []
    # Pre-existing translator warning preserved.
    assert pre_existing[0] in warnings
    # Collision warning appended.
    assert any(
        isinstance(w, dict)
        and w.get("reason") == "copy_pages_page_id_collision"
        for w in warnings
    )


def test_scaffold_spec_preserves_em_dashes_in_blocks():
    """Verbatim source text with em dashes survives intake -> spec
    unchanged. This is the brand-voice carve-out for customer-authored
    copy imported from a source URL."""
    # brand-voice-allow-em-dash: customer-copy carve-out test fixture
    customer_text = "Oak floors\u2014wool blankets\u2014warm air."
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {"path": "/", "markdown_blocks": [customer_text]},
        ]
    })
    spec = scaffold_spec(answers)
    assert customer_text in spec["pages"]["home"]["_copy_blocks"]


# ---------------------------------------------------------------------------
# resolve_spec: layout swap + markdown pre-render
# ---------------------------------------------------------------------------

def test_resolve_spec_swaps_copy_faithful_layout():
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/",
                "markdown_blocks": ["# Home", "Real content."],
            }
        ]
    })
    spec = scaffold_spec(answers)
    rs = resolve_spec(BuildSpec.from_dict(spec))
    # Home page now uses the copy-faithful-default layout, not the
    # tradesperson home archetype layout.
    home_layout = rs.page_layouts["home"]
    assert home_layout["variant_group"] == "copy-faithful"
    # Renders into the page spec under "copy" with copy_html.
    home_spec = rs.spec.pages["home"]
    assert "copy" in home_spec
    assert "<h1>Home</h1>" in home_spec["copy"]["copy_html"]
    assert "<p>Real content.</p>" in home_spec["copy"]["copy_html"]


def test_resolve_spec_promotes_non_archetype_page_to_layout():
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/work-with-me",
                "markdown_blocks": ["# WWM", "Body."],
            }
        ]
    })
    spec = scaffold_spec(answers)
    rs = resolve_spec(BuildSpec.from_dict(spec))
    assert "work-with-me" in rs.page_layouts
    assert rs.page_layouts["work-with-me"]["variant_group"] == "copy-faithful"


def test_resolve_spec_no_copy_blocks_uses_archetype_default():
    """Specs without copy_pages render as before (regression guard)."""
    answers = _base_answers()
    spec = scaffold_spec(answers)
    rs = resolve_spec(BuildSpec.from_dict(spec))
    # Tradesperson home default is NOT copy-faithful.
    assert rs.page_layouts["home"]["variant_group"] != "copy-faithful"


# ---------------------------------------------------------------------------
# emit_page_view: view content
# ---------------------------------------------------------------------------

def test_emit_page_view_includes_raw_markdown_widget():
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/",
                "markdown_blocks": [
                    "# Sample Trades Co",
                    "A sample paragraph of copy.",
                    "- Sample service one",
                ],
            }
        ]
    })
    spec = scaffold_spec(answers)
    files = compose_files(resolve_spec(BuildSpec.from_dict(spec)))
    view = next((c for p, c in files if p == "views/home.ejs"), None)
    assert view is not None
    # Hero-centered + content-raw-markdown widgets in this order.
    assert "widgets/hero/centered.ejs" in view
    assert "widgets/content/raw-markdown.ejs" in view
    # Em dash carve-out: when the customer copy contains em dashes the
    # rendered HTML on the view has them.
    assert "A sample paragraph of copy." in view


def test_emit_page_view_carries_pre_rendered_html_to_widget():
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/",
                "markdown_blocks": ["**Bold** text."],
            }
        ]
    })
    spec = scaffold_spec(answers)
    files = compose_files(resolve_spec(BuildSpec.from_dict(spec)))
    view = next((c for p, c in files if p == "views/home.ejs"), None)
    assert view is not None
    # The rendered HTML appears in the widget data literal. Inside the EJS
    # tag `<` and `>` are written as their JSON \\u escapes (so no value can
    # close the tag); the JS string still evaluates to the same HTML.
    assert "\\u003cstrong\\u003eBold\\u003c/strong\\u003e" in view
    assert "<strong>" not in view


# ---------------------------------------------------------------------------
# Public router emits a route for non-archetype copy pages
# ---------------------------------------------------------------------------

def test_routes_pages_js_emits_route_for_non_archetype_copy_page():
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/work-with-me",
                "markdown_blocks": ["# WWM", "body."],
            }
        ]
    })
    spec = scaffold_spec(answers)
    files = compose_files(resolve_spec(BuildSpec.from_dict(spec)))
    routes = next((c for p, c in files if p == "routes/pages.js"), None)
    assert routes is not None
    assert "router.get('/work-with-me'" in routes


# ---------------------------------------------------------------------------
# Hero-centered widget renders the new fields
# ---------------------------------------------------------------------------

def test_hero_centered_widget_escapes_heading_xss():
    """A crafted heading must NOT inject script tags into the rendered
    H1. Faithful-copy mode passes heading text from a customer source
    URL; the widget must escape end-to-end."""
    import os
    import subprocess

    widget_path = Path(__file__).resolve().parent.parent / "widgets" / "hero" / "centered.ejs"
    repo_root = Path(__file__).resolve().parent.parent.parent
    web_node_modules = repo_root / "web" / "node_modules"
    if not (web_node_modules / "ejs").exists():
        pytest.skip("web/node_modules/ejs not installed; run `cd web && npm install`")
    js = """
const ejs = require('ejs');
const fs = require('fs');
const path = require('path');
const filename = process.argv[1];
const data = JSON.parse(process.argv[2]);
const root = process.argv[3];
const tpl = fs.readFileSync(filename, 'utf8');
process.stdout.write(ejs.render(tpl, data, {filename, root, views: [path.dirname(filename), root]}));
"""
    data = {
        "settings": {"site_name": "Site"},
        "heading": '<script>alert("xss")</script>Hello',
        # Mark faithful-copy mode (presence of either body_html or
        # credential_lines) so the heading takes the strict-escape path.
        "credential_lines": ["x"],
    }
    env = {**os.environ, "NODE_PATH": str(web_node_modules)}
    proc = subprocess.run(
        ["node", "-e", js, "--",
         str(widget_path), json.dumps(data),
         str(widget_path.parent.parent)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    if proc.returncode != 0:
        pytest.skip(f"node + ejs unavailable: {proc.stderr}")
    out = proc.stdout
    # The literal text appears (escaped) but never as a real tag.
    assert "<script>alert" not in out
    assert "&lt;script&gt;alert" in out
    # Make sure the visible text still ends with "Hello" (not just stripped).
    assert "Hello" in out


def test_hero_centered_widget_renders_body_html_and_credentials():
    """Render the hero widget directly with copy-mode fields and
    confirm the output carries body_html, credential lines, and the
    sms cta link."""
    import os
    import subprocess

    widget_path = Path(__file__).resolve().parent.parent / "widgets" / "hero" / "centered.ejs"
    repo_root = Path(__file__).resolve().parent.parent.parent
    web_node_modules = repo_root / "web" / "node_modules"
    if not (web_node_modules / "ejs").exists():
        pytest.skip("web/node_modules/ejs not installed; run `cd web && npm install`")
    # node -e CODE -- a b c  =>  process.argv = [node, a, b, c]
    js = """
const ejs = require('ejs');
const fs = require('fs');
const path = require('path');
const filename = process.argv[1];
const data = JSON.parse(process.argv[2]);
const root = process.argv[3];
const tpl = fs.readFileSync(filename, 'utf8');
process.stdout.write(ejs.render(tpl, data, {filename, root, views: [path.dirname(filename), root]}));
"""
    data = {
        "settings": {"site_name": "Sample Site"},
        "heading": "Sample Trades Co",
        "body_html": "<p>A sample paragraph of copy.</p><p>A second sample paragraph.</p>",
        "credential_lines": ["Sample credential one", "Sample credential two", "Sample credential three"],
        "cta_primary": {"text": "Text me", "href": "sms:5555550112"},
    }
    env = {**os.environ, "NODE_PATH": str(web_node_modules)}
    proc = subprocess.run(
        ["node", "-e", js, "--",
         str(widget_path), json.dumps(data),
         str(widget_path.parent.parent)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    if proc.returncode != 0:
        pytest.skip(f"node + ejs unavailable: {proc.stderr}")
    out = proc.stdout
    assert "Sample Trades Co" in out
    assert "A sample paragraph of copy." in out
    assert "A second sample paragraph." in out
    assert "Sample credential one" in out
    assert "Sample credential two" in out
    assert "Sample credential three" in out
    assert 'href="sms:5555550112"' in out


# ---------------------------------------------------------------------------
# End-to-end smoke
# ---------------------------------------------------------------------------

def test_compose_files_smoke_with_copy_pages():
    """Full compose pipeline runs cleanly with copy_pages set."""
    answers = _base_answers(_harbor_meta={
        "copy_source_url": "https://sample-trades.example/",
        "copy_pages": [
            {
                "path": "/",
                "title": "Home",
                "markdown_blocks": ["# Home", "Para."],
                "hero": {
                    "heading": "Home",
                    "credential_lines": ["Cred"],
                    "cta_text_me_phone": "5555550112",
                },
            },
            {
                "path": "/about",
                "markdown_blocks": ["# About", "Sample about copy."],
            },
        ],
    })
    spec = scaffold_spec(answers)
    bs = BuildSpec.from_dict(spec)
    assert validate_spec(bs) == []
    files = compose_files(resolve_spec(bs))
    paths = [p for p, _c in files]
    assert "views/home.ejs" in paths
    assert "views/about.ejs" in paths


def test_faithful_copy_hero_heading_not_overridden_by_settings_hero_tagline():
    """Regression guard: when faithful-
    copy mode is active, the keeper's verbatim hero heading must NOT be
    clobbered by the archetype-derived `settings.hero_tagline` default
    (which `_page_widget_defaults` injects as an `_EJSRef` that wins over
    spec values).

    The fix: `_page_widget_defaults` returns `{}` when the page has
    `_copy_blocks`. Without the fix, tradesperson-contractor's
    `copy_tone.split(',')[0].strip().title()` -> "Direct" lands on
    `settings.hero_tagline` and the rendered H1 reads "Direct" instead
    of the keeper's actual heading."""
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {
                "path": "/",
                "markdown_blocks": ["# Sample Trades Co", "Sample copy."],
                "hero": {"heading": "Sample Trades Co"},
            }
        ]
    })
    spec = scaffold_spec(answers)
    bs = BuildSpec.from_dict(spec)
    files = dict(compose_files(resolve_spec(bs)))
    home_view = files["views/home.ejs"]
    # The hero include literal must carry heading="Sample Trades Co"
    # directly (or omit heading so the default would apply, which it
    # doesn't here because the spec sets it). The settings.hero_tagline
    # `_EJSRef` overlay must NOT appear.
    assert '"heading": "Sample Trades Co"' in home_view, (
        f"hero heading should be the keeper's verbatim heading, "
        f"not overridden by settings.hero_tagline. Got: {home_view[:600]}"
    )
    assert 'settings.hero_tagline' not in home_view, (
        "faithful-copy home view must not carry the settings.hero_tagline "
        "_EJSRef (would clobber keeper's heading at render time)"
    )


def test_faithful_copy_about_page_hero_not_overridden_by_about_title_setting():
    """Same guard for the /about page. Faithful-copy /about derives its
    heading from `_build_copy_hero_block()` (falling back to the page-id
    title-cased when hero_meta.heading is absent); the
    `settings.about_title` `_EJSRef` overlay must not override."""
    answers = _base_answers(_harbor_meta={
        "copy_pages": [
            {"path": "/about", "markdown_blocks": ["# About Us", "Sample about copy."]},
        ]
    })
    spec = scaffold_spec(answers)
    bs = BuildSpec.from_dict(spec)
    files = dict(compose_files(resolve_spec(bs)))
    about_view = files["views/about.ejs"]
    assert 'settings.about_title' not in about_view, (
        "faithful-copy /about must not carry settings.about_title _EJSRef"
    )
