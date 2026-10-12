"""Unit tests for compose/lint_external_urls.py.

A source crawl can capture retailer URLs that later 404, for example
when a publisher republishes a book under new ASINs. The linter is the
pre-deploy guard for this class of regression. Tests cover:

  * URL extraction from realistic spec shapes (Book model, social URLs,
    nested seed_content, _harbor_meta.copy_source_url, cta_href).
  * Free-text fields are NOT scanned (URLs inside body strings don't
    surface as live links).
  * Internal paths (/contact, /book) are not pinged.
  * URL dedup: same URL in multiple fields = single ping.
  * HEAD→GET fallback when origin answers HEAD with 405 / 403.
  * Network error handling (status=None, ok=False).
  * mode="skip" returns [] without pinging.
  * mode="warn" returns warnings without raising.
  * mode="error" raises ExternalUrlLintError when any URL fails.
  * Empty spec returns [].
  * The walk over the document is iterative: a nesting a recursive walk
    cannot survive is read like any other.
  * Build-app integration: warnings flow into translation-warnings.txt
    + compose-manifest.txt, and the manifest's `external_url_lint:` header
    says whether the lint was skipped, ran or could not run.
"""

from __future__ import annotations

import os
import pytest

from compose.lint_external_urls import (
    ExternalUrlLintError,
    _is_external_http_url,
    check_url,
    extract_external_urls,
    lint_external_urls,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_is_external_http_url_accepts_http_and_https():
    assert _is_external_http_url("http://example.com")
    assert _is_external_http_url("https://example.com/path")


def test_is_external_http_url_rejects_relative_paths():
    assert not _is_external_http_url("/contact")
    assert not _is_external_http_url("/admin/dashboard")


def test_is_external_http_url_rejects_other_schemes():
    assert not _is_external_http_url("mailto:hi@example.com")
    assert not _is_external_http_url("sms:+15555550112")
    assert not _is_external_http_url("tel:+15555550112")
    assert not _is_external_http_url("#anchor")


def test_is_external_http_url_rejects_non_strings():
    assert not _is_external_http_url(None)
    assert not _is_external_http_url(42)
    assert not _is_external_http_url(["http://example.com"])
    assert not _is_external_http_url("")
    assert not _is_external_http_url("   ")


# ---------------------------------------------------------------------------
# extract_external_urls
# ---------------------------------------------------------------------------

def test_extract_empty_spec():
    assert extract_external_urls({}) == []


def test_extract_finds_book_retailer_urls():
    # The author-site case: Books seed rows with retailer
    # URLs scattered across multiple link fields.
    spec = {
        "seed_content": {
            "Book": [
                {
                    "title": "Sample Book One",
                    "amazon_url": "https://www.amazon.example/dp/0000000001",
                    "goodreads_url": "https://www.goodreads.example/book/show/12345",
                    "local_bookstore_url": "https://www.bookstore.example/book/9780000000001",
                },
                {
                    "title": "Second Book",
                    "amazon_url": "https://www.amazon.example/dp/B000000001",
                },
            ],
        },
    }
    urls = extract_external_urls(spec)
    found = {url for _, url in urls}
    assert "https://www.amazon.example/dp/0000000001" in found
    assert "https://www.amazon.example/dp/B000000001" in found
    assert "https://www.goodreads.example/book/show/12345" in found
    assert "https://www.bookstore.example/book/9780000000001" in found


def test_extract_finds_social_urls_in_settings():
    spec = {
        "settings": {
            "instagram_url": "https://instagram.example/sample-author",
            "facebook_url": "https://facebook.example/sample-author",
        },
    }
    urls = extract_external_urls(spec)
    found = {url for _, url in urls}
    assert "https://instagram.example/sample-author" in found
    assert "https://facebook.example/sample-author" in found


def test_extract_finds_harbor_copy_source_url():
    spec = {
        "_harbor_meta": {
            "copy_source_url": "https://oldsite.example.com",
        },
    }
    urls = extract_external_urls(spec)
    assert urls == [("_harbor_meta.copy_source_url", "https://oldsite.example.com")]


def test_extract_finds_cta_href_when_external():
    spec = {
        "pages": {
            "home": {
                "cta": {
                    "cta_href": "https://calendly.example/sample-author/intro",
                },
            },
        },
    }
    urls = extract_external_urls(spec)
    assert urls == [
        ("pages.home.cta.cta_href", "https://calendly.example/sample-author/intro"),
    ]


def test_extract_skips_internal_cta_paths():
    # /contact, /book, /admin etc. are NOT external URLs and must not
    # be pinged. This is the bulk of cta_href values in practice.
    spec = {
        "pages": {
            "home": {
                "offerings": [
                    {"title": "1:1 Sessions", "cta_href": "/contact"},
                    {"title": "Workshops", "cta_href": "/book"},
                ],
            },
        },
    }
    assert extract_external_urls(spec) == []


def test_extract_ignores_urls_inside_free_text_bodies():
    # We only walk known URL field names. A URL embedded inside a
    # `description` or `body` doesn't render as a live link in our
    # widgets, so we don't ping it.
    spec = {
        "pages": {
            "about": {
                "body": "Read more at https://example.com/whatever",
                "description": "See https://elsewhere.example.com for context.",
            },
        },
    }
    assert extract_external_urls(spec) == []


def test_extract_dedupes_same_url_across_fields():
    # nav_logo_url and og_image_url often point at the same CDN
    # asset. Don't ping it twice.
    spec = {
        "settings": {
            "nav_logo_url": "https://cdn.example.com/logo.png",
            "og_image_url": "https://cdn.example.com/logo.png",
        },
    }
    urls = extract_external_urls(spec)
    assert len(urls) == 1
    assert urls[0][1] == "https://cdn.example.com/logo.png"


def test_extract_strips_whitespace():
    spec = {"settings": {"amazon_url": "  https://amazon.example/dp/X  "}}
    urls = extract_external_urls(spec)
    assert urls == [("settings.amazon_url", "https://amazon.example/dp/X")]


def test_extract_walks_nested_lists_and_dicts():
    spec = {
        "data_models": [
            {
                "name": "Book",
                "seed_rows": [
                    {"amazon_url": "https://a.example.com/1"},
                    {"amazon_url": "https://a.example.com/2"},
                ],
            },
        ],
    }
    urls = extract_external_urls(spec)
    found = {url for _, url in urls}
    assert found == {
        "https://a.example.com/1",
        "https://a.example.com/2",
    }


def test_extract_keeps_document_order_and_paths():
    """Siblings are read in the order the document has them: a URL inside
    an earlier container comes before a later key of the parent, and a
    repeated URL keeps the path where it was first seen."""
    spec = {
        "a": {"url": "https://one.example.com/"},
        "image_url": "https://two.example.com/",
        "items": [
            {"href": "https://three.example.com/"},
            "text",
            [{"link": "https://four.example.com/"}],
        ],
        "again": {"url": "https://one.example.com/"},
    }
    assert extract_external_urls(spec) == [
        ("a.url", "https://one.example.com/"),
        ("image_url", "https://two.example.com/"),
        ("items[0].href", "https://three.example.com/"),
        ("items[2][0].link", "https://four.example.com/"),
    ]
    # A list at the top has no dot in front of its first label either.
    assert extract_external_urls([{"url": "https://five.example.com/"}]) == [
        ("[0].url", "https://five.example.com/"),
    ]


def test_extract_survives_a_nesting_a_recursive_walk_cannot():
    """The document is a model's JSON. A 50,000-level value (under a key
    the engine does not even read) used to raise RecursionError here; the
    walk is iterative now, and what it finds at the bottom is reported
    with the whole path."""
    node = {"image_url": "https://deep.example.com/x.png"}
    for _ in range(50_000):
        node = {"k": [node]}
    found = extract_external_urls({"notes": node})
    assert len(found) == 1
    path, url = found[0]
    assert url == "https://deep.example.com/x.png"
    assert path.startswith("notes.k[0].k[0].") and path.endswith(".k[0].image_url")
    assert path.count(".k[0]") == 50_000


def test_extract_does_not_loop_on_a_value_that_refers_to_itself():
    """JSON cannot, but a dict built in memory can."""
    loop: dict = {"image_url": "https://loop.example.com/a.png"}
    loop["again"] = [loop]
    assert extract_external_urls(loop) == [("image_url", "https://loop.example.com/a.png")]


# ---------------------------------------------------------------------------
# check_url with injected opener
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status):
        self.status = status
    def getcode(self):
        return self.status


def test_check_url_treats_200_as_ok():
    calls = []
    def opener(req, timeout=None):
        calls.append((req.get_method(), req.full_url))
        return _FakeResp(200)
    from compose.lint_external_urls import _check_url_via
    result = _check_url_via("https://good.example.com", 5.0, opener)
    assert result["ok"] is True
    assert result["status"] == 200
    # HEAD attempted first
    assert calls[0][0] == "HEAD"


def test_check_url_treats_301_as_ok():
    def opener(req, timeout=None):
        return _FakeResp(301)
    from compose.lint_external_urls import _check_url_via
    result = _check_url_via("https://r.example.com", 5.0, opener)
    assert result["ok"] is True
    assert result["status"] == 301


def test_check_url_404_marks_not_ok():
    import urllib.error
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, None,
        )
    from compose.lint_external_urls import _check_url_via
    result = _check_url_via("https://gone.example.com", 5.0, opener)
    assert result["ok"] is False
    assert result["status"] == 404


def test_check_url_falls_back_to_get_on_405():
    import urllib.error
    calls = []
    def opener(req, timeout=None):
        calls.append(req.get_method())
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError(
                req.full_url, 405, "Method Not Allowed", {}, None,
            )
        return _FakeResp(200)
    from compose.lint_external_urls import _check_url_via
    result = _check_url_via("https://amazon.example.com", 5.0, opener)
    assert result["ok"] is True
    assert result["status"] == 200
    assert calls == ["HEAD", "GET"]


def test_check_url_falls_back_to_get_on_403():
    import urllib.error
    calls = []
    def opener(req, timeout=None):
        calls.append(req.get_method())
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, None,
            )
        return _FakeResp(200)
    from compose.lint_external_urls import _check_url_via
    result = _check_url_via("https://cdn.example.com", 5.0, opener)
    assert result["ok"] is True
    assert calls == ["HEAD", "GET"]


def test_check_url_network_error_collapses_to_not_ok():
    import urllib.error
    def opener(req, timeout=None):
        raise urllib.error.URLError("nodename nor servname provided")
    from compose.lint_external_urls import _check_url_via
    result = _check_url_via("https://dns-broken.example.invalid", 5.0, opener)
    assert result["ok"] is False
    assert result["status"] is None
    assert "URLError" in result["error"]


# ---------------------------------------------------------------------------
# lint_external_urls: mode + warning shape
# ---------------------------------------------------------------------------

def _fake_check(returns):
    """Build a check_fn that returns predetermined results by URL."""
    def _check(url):
        return returns.get(url, {"url": url, "ok": True, "status": 200, "error": None})
    return _check


def test_lint_skip_mode_returns_empty_without_pinging():
    called = []
    def check_fn(url):
        called.append(url)
        return {"url": url, "ok": False, "status": 404, "error": "HTTP 404"}
    spec = {"settings": {"amazon_url": "https://amazon.example.com/dp/X"}}
    result = lint_external_urls(spec, mode="skip", check_fn=check_fn)
    assert result == []
    assert called == []


def test_lint_warn_mode_returns_warnings_without_raising():
    spec = {
        "seed_content": {
            "Book": [
                {"amazon_url": "https://amazon.example.com/dp/DEAD"},
                {"amazon_url": "https://amazon.example.com/dp/LIVE"},
            ],
        },
    }
    check_fn = _fake_check({
        "https://amazon.example.com/dp/DEAD": {
            "url": "https://amazon.example.com/dp/DEAD",
            "ok": False,
            "status": 404,
            "error": "HTTP 404",
        },
    })
    warnings = lint_external_urls(spec, mode="warn", check_fn=check_fn)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["reason"] == "external_url_unreachable"
    assert "DEAD" in w["detail"]
    assert "status=404" in w["detail"]


def test_lint_error_mode_raises_on_any_failure():
    spec = {"settings": {"amazon_url": "https://amazon.example.com/dp/DEAD"}}
    check_fn = _fake_check({
        "https://amazon.example.com/dp/DEAD": {
            "url": "https://amazon.example.com/dp/DEAD",
            "ok": False,
            "status": 404,
            "error": "HTTP 404",
        },
    })
    with pytest.raises(ExternalUrlLintError):
        lint_external_urls(spec, mode="error", check_fn=check_fn)


def test_lint_error_mode_does_not_raise_when_all_pass():
    spec = {"settings": {"amazon_url": "https://amazon.example.com/dp/LIVE"}}
    check_fn = _fake_check({})  # default = ok
    assert lint_external_urls(spec, mode="error", check_fn=check_fn) == []


def test_lint_unknown_mode_falls_back_to_warn():
    spec = {"settings": {"amazon_url": "https://amazon.example.com/dp/DEAD"}}
    check_fn = _fake_check({
        "https://amazon.example.com/dp/DEAD": {
            "url": "https://amazon.example.com/dp/DEAD",
            "ok": False,
            "status": 404,
            "error": "HTTP 404",
        },
    })
    # mode="banana" should not crash; falls back to warn (returns warnings,
    # does NOT raise).
    warnings = lint_external_urls(spec, mode="banana", check_fn=check_fn)
    assert len(warnings) == 1


def test_lint_reads_env_var_when_mode_unspecified(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")
    called = []
    def check_fn(url):
        called.append(url)
        return {"url": url, "ok": True, "status": 200, "error": None}
    spec = {"settings": {"amazon_url": "https://amazon.example.com/dp/X"}}
    # mode defaults to env var = skip
    assert lint_external_urls(spec, check_fn=check_fn) == []
    assert called == []


def test_lint_warning_includes_field_path():
    spec = {
        "seed_content": {
            "Book": [
                {"amazon_url": "https://amazon.example.com/dp/DEAD"},
            ],
        },
    }
    check_fn = _fake_check({
        "https://amazon.example.com/dp/DEAD": {
            "url": "https://amazon.example.com/dp/DEAD",
            "ok": False,
            "status": 404,
            "error": "HTTP 404",
        },
    })
    warnings = lint_external_urls(spec, mode="warn", check_fn=check_fn)
    assert "seed_content.Book[0].amazon_url" in warnings[0]["detail"]


def test_lint_warning_handles_network_error_without_status():
    spec = {"settings": {"amazon_url": "https://gone.example.invalid"}}
    check_fn = _fake_check({
        "https://gone.example.invalid": {
            "url": "https://gone.example.invalid",
            "ok": False,
            "status": None,
            "error": "URLError: nodename nor servname provided",
        },
    })
    warnings = lint_external_urls(spec, mode="warn", check_fn=check_fn)
    assert len(warnings) == 1
    assert "no-response" in warnings[0]["detail"]
    assert "URLError" in warnings[0]["detail"]


def test_lint_skips_when_no_external_urls():
    spec = {
        "pages": {
            "home": {
                "cta": {"cta_href": "/contact"},
                "offerings": [{"cta_href": "/book"}],
            },
        },
    }
    check_fn = _fake_check({})
    assert lint_external_urls(spec, mode="warn", check_fn=check_fn) == []


# ---------------------------------------------------------------------------
# build_app integration: warnings flow into manifest + warnings file
# ---------------------------------------------------------------------------

def _lint_header(manifest: str) -> str:
    """The value of the manifest's one `external_url_lint:` header line."""
    lines = [l for l in manifest.split("\n") if l.startswith("external_url_lint: ")]
    assert len(lines) == 1, lines
    return lines[0].split(": ", 1)[1]


def test_build_app_records_link_warnings_in_manifest(tmp_path, monkeypatch):
    """End-to-end: dead external URL in spec → build_app emits a warning
    line in compose-manifest.txt and a translation-warnings.txt file.
    """
    # Force "warn" mode for THIS test (overrides the conftest "skip" default).
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "warn")

    # Patch check_url so we never hit the network. The patch lives at the
    # `compose.lint_external_urls.check_url` import site so the default
    # check_fn injection in lint_external_urls picks it up.
    import compose.lint_external_urls as mod
    def fake_check(url, timeout=5.0):
        if "DEAD" in url:
            return {"url": url, "ok": False, "status": 404, "error": "HTTP 404"}
        return {"url": url, "ok": True, "status": 200, "error": None}
    monkeypatch.setattr(mod, "check_url", fake_check)

    from compose.build import build_app

    spec = {
        "app_name": "link-check-fixture",
        "archetype": "author",
        "admin_email": "admin@example.com",
        "data_models": [],
        "pages": {},
        "settings": {
            "amazon_url": "https://amazon.example.com/dp/DEAD",
            "instagram_url": "https://instagram.example.com/LIVE",
        },
    }
    out = build_app(spec, output_dir=tmp_path / "linkcheck")
    manifest = (out / "compose-manifest.txt").read_text()
    assert "external_url_unreachable" in manifest
    assert "DEAD" in manifest
    # The translation-warnings file should also exist when any warning fired.
    warnings_file = out / "translation-warnings.txt"
    assert warnings_file.exists()
    body = warnings_file.read_text()
    assert "external_url_unreachable" in body
    assert "DEAD" in body
    # The healthy URL should NOT be flagged.
    assert "instagram.example.com/LIVE" not in body or "external_url_unreachable" not in body.split("LIVE")[0]


def test_build_app_no_manifest_warning_when_all_urls_live(tmp_path, monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "warn")
    import compose.lint_external_urls as mod
    monkeypatch.setattr(
        mod, "check_url",
        lambda url, timeout=5.0: {"url": url, "ok": True, "status": 200, "error": None},
    )
    from compose.build import build_app

    spec = {
        "app_name": "link-ok-fixture",
        "archetype": "author",
        "admin_email": "admin@example.com",
        "data_models": [],
        "pages": {},
        "settings": {
            "amazon_url": "https://amazon.example.com/dp/LIVE",
        },
    }
    out = build_app(spec, output_dir=tmp_path / "linkok")
    manifest = (out / "compose-manifest.txt").read_text()
    assert "external_url_unreachable" not in manifest
    # A lint that ran and found nothing says so.
    assert _lint_header(manifest) == "ran"


def test_build_app_skip_mode_does_not_ping(tmp_path, monkeypatch):
    """When env var=skip, no URLs are pinged and no warnings emitted."""
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")
    called = []
    import compose.lint_external_urls as mod
    def check_fn(url, timeout=5.0):
        called.append(url)
        return {"url": url, "ok": False, "status": 404, "error": "HTTP 404"}
    monkeypatch.setattr(mod, "check_url", check_fn)

    from compose.build import build_app
    spec = {
        "app_name": "link-skip-fixture",
        "archetype": "author",
        "admin_email": "admin@example.com",
        "data_models": [],
        "pages": {},
        "settings": {
            "amazon_url": "https://amazon.example.com/dp/DEAD",
        },
    }
    out = build_app(spec, output_dir=tmp_path / "linkskip")
    assert called == []  # no pings fired
    manifest = (out / "compose-manifest.txt").read_text()
    assert "external_url_unreachable" not in manifest
    # ... and the report does not read like a lint that ran clean.
    assert _lint_header(manifest) == "skipped"


def test_build_app_error_mode_refuses_to_complete_on_dead_link(tmp_path, monkeypatch):
    """CAS_LINT_EXTERNAL_URLS=error → ExternalUrlLintError propagates."""
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "error")
    import compose.lint_external_urls as mod
    monkeypatch.setattr(
        mod, "check_url",
        lambda url, timeout=5.0: {"url": url, "ok": False, "status": 404, "error": "HTTP 404"},
    )
    from compose.build import build_app
    spec = {
        "app_name": "link-err-fixture",
        "archetype": "author",
        "admin_email": "admin@example.com",
        "data_models": [],
        "pages": {},
        "settings": {
            "amazon_url": "https://amazon.example.com/dp/DEAD",
        },
    }
    with pytest.raises(ExternalUrlLintError):
        build_app(spec, output_dir=tmp_path / "linkerr")


def test_build_app_lint_failure_collapses_to_single_warning(tmp_path, monkeypatch):
    """If the linter itself raises (egress blocked etc.) under warn mode,
    build_app records ONE warning and keeps going; never hard-fails."""
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "warn")
    import compose.build as bm
    def boom(spec, mode=None, check_fn=None):
        raise RuntimeError("simulated total network egress block")
    monkeypatch.setattr(bm, "lint_external_urls", boom)

    from compose.build import build_app
    spec = {
        "app_name": "link-failopen-fixture",
        "archetype": "author",
        "admin_email": "admin@example.com",
        "data_models": [],
        "pages": {},
        "settings": {
            "amazon_url": "https://amazon.example.com/dp/ANY",
        },
    }
    out = build_app(spec, output_dir=tmp_path / "linkfailopen")
    manifest = (out / "compose-manifest.txt").read_text()
    assert "external_url_lint_error" in manifest
    assert "simulated total network egress block" in manifest
    # A lint that could not run is not a lint that ran clean: the header
    # says so as well as the warning line.
    assert _lint_header(manifest) == "failed"
