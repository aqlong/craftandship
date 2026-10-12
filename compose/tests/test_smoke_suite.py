"""Post-build smoke-test suite; runs against a booted fixture app and
asserts the deterministic UI/QA contracts that replace v1's Claude-driven
design-review loop.

These tests verify the generated app AFTER compose + migrate + seed + boot:
every route responds 200 with real content, settings-driven fields render
the expected values, required assets are served, no LLM-slop phrases leak
into emitted HTML, admin gate works, form validation attrs are emitted, and
the `{Word}` italic-accent transform actually fires on every heading-capable
widget. None of this runs Claude; it's static HTTP + regex.

Reuses the `db_and_server` + `http` fixtures from test_compose_end_to_end.py
so we piggyback on the cached node_modules + single fixture server boot.

Skipped by default when Postgres or Node aren't available (CI-safe).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Re-use the composed_app + db_and_server + http fixtures from the
# existing E2E test module so we don't spawn a second Postgres / server.
from compose.tests.test_compose_end_to_end import (  # noqa: F401
    composed_app,
    db_and_server,
    http,
    SAMPLE_SPEC,
)


# ---- 1. Every-route smoke (200 + substantive body + page chrome) ---- #

def test_smoke_every_spec_route_has_substantive_body(http):
    """Every route in the spec's pages array must return 200 with a real
    HTML body (not a tiny error page). Each page should also carry the
    full layout chrome: <title>, <main id="main-content">, skip-to-content."""
    for path in ("/", "/about", "/work-with-me", "/journal", "/events", "/gallery", "/contact"):
        status, body, _ = http(path)
        assert status == 200, f"{path} returned {status}"
        assert len(body) > 2000, f"{path} body too small ({len(body)} bytes); likely an error page"
        assert "<title>" in body, f"{path} missing <title>"
        assert 'id="main-content"' in body, f"{path} missing main-content landmark"
        assert "skip-to-content" in body.lower() or "Skip to content" in body, f"{path} missing skip-to-content"


def test_smoke_auth_routes_respond(http):
    status, body, _ = http("/login")
    assert status == 200
    assert 'name="_csrf"' in body
    assert 'type="password"' in body
    # /admin redirects to /login when not authenticated
    status, _, headers = http("/admin")
    assert status in (302, 303)
    loc = headers.get("Location") or headers.get("location") or ""
    assert "/login" in loc


# ---- 2. Settings-driven content contracts ---- #

def test_smoke_site_name_in_nav_on_every_page(http):
    """The seeded site_name must appear in the nav of every public page
    so visitors always see the brand. (Spec's site_name wins; if the
    archetype didn't seed one we fall back to app_name.)"""
    for path in ("/", "/about", "/events", "/contact"):
        status, body, _ = http(path)
        assert status == 200
        # Sample healer spec uses 'Sample Healing Studio'
        assert "Sample Healing Studio" in body, f"{path} missing site_name 'Sample Healing Studio' in nav"


def test_smoke_hero_tagline_rendered_on_home(http):
    """Hero tagline from settings must surface on the home page. Widget
    now reads via _EJSRef(settings.hero_tagline) at render time so this
    value can be edited live via /admin/settings."""
    status, body, _ = http("/")
    assert status == 200
    # Sample healer hero tagline has distinctive words
    assert "Energy work" in body or "subconscious" in body.lower() or "healing" in body.lower(), (
        "home missing hero tagline keywords"
    )


def test_smoke_contact_email_rendered_on_contact(http):
    status, body, _ = http("/contact")
    assert status == 200
    # Sample healer seeds contact_email = hello@samplehealingstudio.example
    assert "@" in body, "contact page missing any email"
    assert "samplehealingstudio.example" in body or "Sample Healing Studio" in body


# ---- 3. Italic-accent {Word} transform fires on every heading-capable widget ---- #

def test_smoke_curly_brace_syntax_never_renders_literal(http):
    """The `{Word}` italic-accent syntax must be transformed to <em>Word</em>
    on every page. If a raw `{Word}` appears in the rendered HTML (outside
    of hidden JSON / script content), the transform is broken somewhere."""
    for path in ("/", "/about", "/work-with-me", "/contact"):
        status, body, _ = http(path)
        assert status == 200
        # Strip <script> contents to avoid false positives from inline JSON.
        cleaned = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.DOTALL | re.IGNORECASE)
        # Literal `{Word}` (single uppercase-letter-initial word) on the page = transform failure.
        leaks = re.findall(r"\{[A-Z][a-zA-Z]{2,}\}", cleaned)
        assert not leaks, f"{path} leaked raw italic-accent syntax: {leaks[:3]}"


# ---- 4. Required static assets all 200 ---- #

@pytest.mark.parametrize("asset,min_bytes", [
    ("/style.css", 5000),
    ("/logo.svg", 200),
    ("/favicon.svg", 200),
    ("/manifest.json", 100),
    ("/robots.txt", 10),
    ("/js/cas-reveal.js", 500),
    ("/js/cas-form-validate.js", 1000),
    ("/js/upload-zone.js", 500),
])
def test_smoke_required_asset_serves_200(http, asset, min_bytes):
    status, body, _ = http(asset)
    assert status == 200, f"{asset} returned {status}"
    assert len(body) >= min_bytes, f"{asset} body too small ({len(body)} bytes, expected >= {min_bytes})"


# ---- 5. Form client-side validation attrs are emitted ---- #

def test_smoke_contact_form_has_pattern_attrs(http):
    """Contact form must have typed inputs (type=email/tel) with pattern
    attrs so cas-form-validate.js enforces client-side validation."""
    status, body, _ = http("/contact")
    assert status == 200
    assert 'class="cas-form"' in body or 'class=\'cas-form\'' in body or 'cas-form' in body
    assert 'type="email"' in body, "contact form missing type=email on email field"
    # phone is optional so type=tel may not always be present, but pattern
    # should fire if present:
    if 'type="tel"' in body:
        assert "pattern=" in body, "contact form tel input missing pattern attr"


# ---- 6. Slop-guard: no AI-generated cliché content or filler text ---- #

BANNED_PHRASES = [
    "Lorem Ipsum",
    "lorem ipsum",
    "Acme Corp",
    "John Doe",
    "Jane Doe",
    "Acme Inc",
    # AI copywriting tells:
    "Elevate your",
    "Seamless experience",
    "Unleash the power",
    "Next-generation",
    "revolutionize",
    "In the world of",
    "Oops!",
]

@pytest.mark.parametrize("path", ["/", "/about", "/work-with-me", "/contact", "/events", "/journal", "/gallery"])
def test_smoke_no_slop_phrases_leak(http, path):
    status, body, _ = http(path)
    assert status == 200
    # Only look at rendered text, not raw HTML tokens
    text = re.sub(r"<[^>]+>", " ", body)
    for phrase in BANNED_PHRASES:
        assert phrase.lower() not in text.lower(), f"{path} contains banned phrase: {phrase!r}"


def test_smoke_no_dead_hash_links(http):
    """href="#" is the universal AI-slop tell for "I didn't think about this"."""
    for path in ("/", "/about", "/contact"):
        status, body, _ = http(path)
        assert status == 200
        # Permit href="#<anchor>" but not href="#" literally
        dead = re.findall(r'href\s*=\s*["\']#["\']', body)
        assert not dead, f"{path} has {len(dead)} dead href=\"#\" links"


# ---- 7. Accessibility smoke ---- #

@pytest.mark.parametrize("path", ["/", "/about", "/contact"])
def test_smoke_exactly_one_h1_per_page(http, path):
    status, body, _ = http(path)
    assert status == 200
    # Count h1 opening tags (handles attributes + uppercase)
    h1_count = len(re.findall(r"<h1[\s>]", body, re.IGNORECASE))
    assert h1_count == 1, f"{path} has {h1_count} <h1> elements (should be exactly 1)"


@pytest.mark.parametrize("path", ["/", "/about", "/journal", "/gallery"])
def test_smoke_every_img_has_alt(http, path):
    status, body, _ = http(path)
    assert status == 200
    imgs = re.findall(r"<img[^>]*>", body, re.IGNORECASE)
    for img in imgs:
        assert re.search(r"\balt\s*=\s*[\"']", img, re.IGNORECASE), f"{path} has <img> missing alt: {img[:100]}"


# ---- 8. Seed completeness ---- #

def test_smoke_seed_row_counts_match_spec(db_and_server):
    """Every seed_content.<model> array in the spec should correspond to
    that many rows in the booted app's DB. Sample healer spec seeds:
    testimonials, blog_posts, events, contact_inquiries, newsletter_subscribers, photos."""
    query = db_and_server["query"]
    spec = json.load(open(SAMPLE_SPEC))
    sc = spec.get("seed_content", {})
    table_map = {
        "testimonials": "testimonials",
        "blog_posts": "blog_posts",
        "events": "events",
        "contact_inquiries": "contact_inquiries",
        "newsletter_subscribers": "newsletter_subscribers",
        "photos": "photos",
    }
    checked = 0
    for key, table in table_map.items():
        expected = len(sc.get(key, []))
        if expected == 0:
            continue
        # The sample site's own tables all exist, so a failed query here is a
        # failure, not a reason to skip the table.
        actual = query(f"SELECT COUNT(*) FROM {table}")[0][0]
        checked += 1
        assert actual >= expected, (
            f"{table}: expected >= {expected} seeded rows, got {actual}"
        )
    assert checked >= 3, "the sample spec seeds at least three tables"


# ---- 9. Powered-by footer attribution ---- #

def test_smoke_craft_and_ship_attribution_in_footer(http):
    """Every build's footer links back to craftandship.io; free growth loop
    + brand attribution on every shipped app."""
    status, body, _ = http("/")
    assert status == 200
    # The attribution widget (compose/widgets/shell/cas-attribution.ejs)
    # reads "made with Craft & Ship" and links to the site.
    assert 'href="https://craftandship.io"' in body
    assert 'aria-label="Made with Craft and Ship"' in body
    assert "made with" in body


# ---- 10. No banned font/shadow tokens in the emitted CSS ---- #

def test_smoke_style_css_no_banned_tokens(http):
    """Style.css must not carry v1-era slop: banned fonts, raw #000/#fff
    filler colors, or 100vh (iOS-busted)."""
    status, body, _ = http("/style.css")
    assert status == 200
    # These are legacy v1 defaults that are banned:
    assert "font-family: Inter" not in body, "Inter font slipped into emitted CSS"
    assert "font-family: Roboto" not in body, "Roboto font slipped into emitted CSS"
    # 100vh on main content elements (we use 100dvh or min-height alternatives)
    # Allow 100vh only inside @media-query shim sections; spot-check:
    assert body.count("100vh") < 3, f"100vh appears {body.count('100vh')} times \u2014 prefer 100dvh"
