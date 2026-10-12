"""Tests for _ensure_sitemap_xml (generated-apps sitemap emitter).

Generated apps had no sitemap, hurting SEO and
the polish bar vs. hand-built competitors. Post-processor emits
public/sitemap.xml from spec.pages with auth_required=False, excluding
admin/API surfaces and auth transactional pages.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: unit tests for page collection + base-URL derivation
# ---------------------------------------------------------------------------


def test_sitemap_pages_keeps_public_paths_in_order():
    spec = {
        "pages": [
            {"path": "/", "auth_required": False},
            {"path": "/about", "auth_required": False},
            {"path": "/contact", "auth_required": False},
        ]
    }
    assert build._sitemap_pages(spec) == ["/", "/about", "/contact"]


def test_sitemap_pages_drops_auth_required():
    spec = {
        "pages": [
            {"path": "/", "auth_required": False},
            {"path": "/dashboard", "auth_required": True},
            {"path": "/account", "auth_required": True},
            {"path": "/blog", "auth_required": False},
        ]
    }
    assert build._sitemap_pages(spec) == ["/", "/blog"]


def test_sitemap_pages_drops_admin_and_api():
    spec = {
        "pages": [
            {"path": "/", "auth_required": False},
            {"path": "/admin", "auth_required": False},
            {"path": "/admin/settings", "auth_required": False},
            {"path": "/api/v1/foo", "auth_required": False},
        ]
    }
    assert build._sitemap_pages(spec) == ["/"]


def test_sitemap_pages_drops_auth_transactional_paths():
    """Login / reset / verify surfaces are auth_required=False (public
    access) but MUST NOT end up in sitemap."""
    spec = {
        "pages": [
            {"path": "/", "auth_required": False},
            {"path": "/login", "auth_required": False},
            {"path": "/signup", "auth_required": False},
            {"path": "/logout", "auth_required": False},
            {"path": "/forgot-password", "auth_required": False},
            {"path": "/reset-password", "auth_required": False},
            {"path": "/verify-email", "auth_required": False},
            {"path": "/contact", "auth_required": False},
        ]
    }
    assert build._sitemap_pages(spec) == ["/", "/contact"]


def test_sitemap_pages_drops_parameterized_paths():
    """/events/:slug is a route pattern, not a URL. Do not list it."""
    spec = {
        "pages": [
            {"path": "/events", "auth_required": False},
            {"path": "/events/:slug", "auth_required": False},
        ]
    }
    assert build._sitemap_pages(spec) == ["/events"]


def test_sitemap_pages_dedupes():
    spec = {
        "pages": [
            {"path": "/about", "auth_required": False},
            {"path": "/about", "auth_required": False},
        ]
    }
    assert build._sitemap_pages(spec) == ["/about"]


def test_sitemap_pages_empty_spec():
    assert build._sitemap_pages({}) == []
    assert build._sitemap_pages({"pages": None}) == []
    assert build._sitemap_pages({"pages": "not a list"}) == []


def test_sitemap_base_url_prefers_settings_site_url():
    spec = {"settings": {"site_url": "https://sample-practitioner.example"}}
    assert build._sitemap_base_url(spec) == "https://sample-practitioner.example"


def test_sitemap_base_url_strips_trailing_slash():
    spec = {"settings": {"site_url": "https://example.org/"}}
    assert build._sitemap_base_url(spec) == "https://example.org"


def test_sitemap_base_url_falls_back_to_placeholder():
    assert build._sitemap_base_url({}) == "https://example.com"
    assert build._sitemap_base_url({"settings": {}}) == "https://example.com"


# ---------------------------------------------------------------------------
# Processor: end-to-end emission against pipeline artifacts
# ---------------------------------------------------------------------------


def _minimal_pages_spec() -> dict:
    return {
        "app_name": "sample-practitioner-v1-123456",
        "settings": {"site_url": "https://sample-practitioner.example"},
        "pages": [
            {"path": "/", "auth_required": False},
            {"path": "/about", "auth_required": False},
            {"path": "/blog", "auth_required": False},
            {"path": "/contact", "auth_required": False},
            {"path": "/admin", "auth_required": False},  # excluded
            {"path": "/login", "auth_required": False},  # excluded
            {"path": "/dashboard", "auth_required": True},  # excluded
        ],
    }


def test_emits_sitemap_with_expected_public_urls():
    spec = _minimal_pages_spec()
    files = [("server.js", "// existing")]
    out = dict(build._ensure_sitemap_xml(files, spec))
    assert "public/sitemap.xml" in out
    sitemap = out["public/sitemap.xml"]
    # Structural guards.
    assert sitemap.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"' in sitemap
    assert "<urlset" in sitemap and "</urlset>" in sitemap
    # Expected URLs present.
    for path in ("/", "/about", "/blog", "/contact"):
        assert f"<loc>https://sample-practitioner.example{path}</loc>" in sitemap
    # Excluded paths absent.
    assert "/admin" not in sitemap
    assert "/login" not in sitemap
    assert "/dashboard" not in sitemap


def test_sitemap_uses_placeholder_base_when_site_url_missing():
    spec = {
        "pages": [
            {"path": "/", "auth_required": False},
            {"path": "/about", "auth_required": False},
        ]
    }
    out = dict(build._ensure_sitemap_xml([], spec))
    sitemap = out["public/sitemap.xml"]
    assert "<loc>https://example.com/</loc>" in sitemap
    assert "<loc>https://example.com/about</loc>" in sitemap


def test_sitemap_noop_when_already_present():
    """Do not clobber a Claude-generated or prior-pass sitemap."""
    existing = "<urlset>existing</urlset>"
    files = [("public/sitemap.xml", existing)]
    spec = _minimal_pages_spec()
    out = dict(build._ensure_sitemap_xml(files, spec))
    assert out["public/sitemap.xml"] == existing


def test_sitemap_noop_when_spec_has_no_pages():
    """Spec without spec.pages → no sitemap emitted (avoids empty XML)."""
    spec = {"app_name": "foo", "settings": {}}
    out = dict(build._ensure_sitemap_xml([], spec))
    assert "public/sitemap.xml" not in out


def test_sitemap_emission_is_idempotent():
    spec = _minimal_pages_spec()
    once = dict(build._ensure_sitemap_xml([], spec))
    twice = dict(build._ensure_sitemap_xml(list(once.items()), spec))
    assert once == twice


def test_sitemap_xml_escapes_ampersands_in_paths():
    """Paths containing `&`, `<`, or `>` MUST be XML-escaped so the
    emitted sitemap is well-formed. Post-review hardening 2026-04-20."""
    spec = {
        "settings": {"site_url": "https://example.com"},
        "pages": [
            {"path": "/search?q=a&b", "auth_required": False},
            {"path": "/page<raw>", "auth_required": False},
        ],
    }
    out = dict(build._ensure_sitemap_xml([], spec))
    sitemap = out["public/sitemap.xml"]
    # Raw unescaped `&` / `<` MUST NOT appear inside a <loc> body.
    assert "<loc>https://example.com/search?q=a&b</loc>" not in sitemap
    assert "<loc>https://example.com/page<raw></loc>" not in sitemap
    # XML-escaped forms DO appear.
    assert "https://example.com/search?q=a&amp;b" in sitemap
    assert "https://example.com/page&lt;raw&gt;" in sitemap


def test_sitemap_xml_escapes_ampersand_in_base_url():
    """Base URL with a trailing query-string ampersand is also
    XML-escaped (defensive, spec.settings.site_url is admin-editable)."""
    spec = {
        "settings": {"site_url": "https://example.com?ref=a&b"},
        "pages": [{"path": "/", "auth_required": False}],
    }
    out = dict(build._ensure_sitemap_xml([], spec))
    sitemap = out["public/sitemap.xml"]
    assert "ref=a&b/" not in sitemap
    assert "ref=a&amp;b/" in sitemap


# ---------------------------------------------------------------------------
# Pipeline registration + ordering
# ---------------------------------------------------------------------------


def test_sitemap_processor_registered_for_build_and_reapply():
    import pipeline
    assert "_ensure_sitemap_xml" in pipeline.REGISTRY
    proc = pipeline.REGISTRY["_ensure_sitemap_xml"]
    assert "build" in proc.phase
    assert "reapply" in proc.phase


def test_sitemap_in_expected_processors():
    import pipeline
    assert "_ensure_sitemap_xml" in pipeline.EXPECTED_PROCESSORS


def test_sitemap_runs_after_favicon_ico_fallback():
    """Order-preservation so the static-asset post-processors run as
    a coherent block in pipeline_order."""
    import pipeline
    order = pipeline.pipeline_order("build")
    ico_pos = order.index("_ensure_favicon_ico_fallback")
    sitemap_pos = order.index("_ensure_sitemap_xml")
    assert ico_pos < sitemap_pos
