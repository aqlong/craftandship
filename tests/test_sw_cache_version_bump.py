"""Tests for _bump_service_worker_cache_version (service-worker-cache-
stickiness fix).

Generated apps register public/sw.js which aggressively caches assets
using the template's fixed CACHE_NAME='app-shell-v1'. After an AI Fix
rewrites views, clients keep serving the stale shell until they hard-
reload. The post-processor stamps a content-hash-derived cache name so
every content change automatically invalidates client caches via the
service worker's `activate` handler.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


SW_TEMPLATE = (
    "const CACHE_NAME = 'app-shell-v1';\n"
    "const SHELL_ASSETS = ['/', '/style.css', '/favicon.svg'];\n"
    "self.addEventListener('install', () => {});\n"
)


# ---------------------------------------------------------------------------
# Hash helper: deterministic + insensitive to sw.js self-reference
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic_for_same_input():
    files = [
        ("server.js", "console.log('a');"),
        ("views/home.ejs", "<h1>Hi</h1>"),
    ]
    h1 = build._content_hash_for_sw_cache(files)
    h2 = build._content_hash_for_sw_cache(list(files))
    assert h1 == h2
    assert len(h1) == 12
    assert all(c in "0123456789abcdef" for c in h1)


def test_content_hash_changes_when_content_changes():
    before = [("server.js", "console.log('a');")]
    after = [("server.js", "console.log('a');\nconsole.log('b');")]
    assert build._content_hash_for_sw_cache(before) != build._content_hash_for_sw_cache(after)


def test_content_hash_ignores_sw_js_itself():
    """The hash MUST NOT depend on sw.js content, otherwise stamping a
    new CACHE_NAME inside sw.js would feed back into the hash on the
    next pass and the two-pass output would diverge."""
    a = [
        ("server.js", "x"),
        ("public/sw.js", "const CACHE_NAME = 'whatever-1';"),
    ]
    b = [
        ("server.js", "x"),
        ("public/sw.js", "const CACHE_NAME = 'whatever-2';"),
    ]
    assert build._content_hash_for_sw_cache(a) == build._content_hash_for_sw_cache(b)


def test_content_hash_ignores_binary_values():
    """Bytes-valued files (binary assets like favicon.ico) must not
    crash the digest and must not alter the hash."""
    a = [("server.js", "x")]
    b = [
        ("server.js", "x"),
        ("public/favicon.ico", b"\x00\x00\x01\x00binary"),
    ]
    assert build._content_hash_for_sw_cache(a) == build._content_hash_for_sw_cache(b)


def test_content_hash_accepts_dict_input():
    d = {"server.js": "x", "views/home.ejs": "<h1>Hi</h1>"}
    l = list(d.items())
    assert build._content_hash_for_sw_cache(d) == build._content_hash_for_sw_cache(l)


# ---------------------------------------------------------------------------
# Processor: stamps CACHE_NAME into sw.js
# ---------------------------------------------------------------------------


def test_cache_name_stamped_with_hash_prefix():
    files = [
        ("public/sw.js", SW_TEMPLATE),
        ("server.js", "// build"),
    ]
    out = dict(build._bump_service_worker_cache_version(files, {}))
    sw = out["public/sw.js"]
    # CACHE_NAME was replaced with app-shell-<12-hex> form.
    import re
    m = re.search(r"const CACHE_NAME\s*=\s*'app-shell-([0-9a-f]{12})'", sw)
    assert m, f"cache name not stamped correctly; sw.js = {sw!r}"
    # Legacy literal is gone.
    assert "'app-shell-v1'" not in sw


def test_cache_name_is_unique_per_build_content():
    """Build A and Build B with different server.js must produce
    different cache names."""
    build_a = [
        ("public/sw.js", SW_TEMPLATE),
        ("server.js", "// version A"),
    ]
    build_b = [
        ("public/sw.js", SW_TEMPLATE),
        ("server.js", "// version B"),
    ]
    out_a = dict(build._bump_service_worker_cache_version(build_a, {}))
    out_b = dict(build._bump_service_worker_cache_version(build_b, {}))
    assert out_a["public/sw.js"] != out_b["public/sw.js"]
    import re
    name_a = re.search(r"'app-shell-([0-9a-f]{12})'", out_a["public/sw.js"]).group(1)
    name_b = re.search(r"'app-shell-([0-9a-f]{12})'", out_b["public/sw.js"]).group(1)
    assert name_a != name_b


def test_noop_when_no_sw_js():
    """Apps without a service worker are untouched."""
    files = [("server.js", "// no sw")]
    out = build._bump_service_worker_cache_version(files, {})
    # Returned verbatim: no sw.js added.
    assert "public/sw.js" not in dict(out)


def test_noop_when_sw_lacks_cache_name():
    """If the sw.js template is so custom that it lacks CACHE_NAME at
    all, do not inject one, bail rather than break the file."""
    custom_sw = "self.addEventListener('install', () => {}); // no cache\n"
    files = [
        ("public/sw.js", custom_sw),
        ("server.js", "// build"),
    ]
    out = dict(build._bump_service_worker_cache_version(files, {}))
    assert out["public/sw.js"] == custom_sw


def test_idempotent_on_unchanged_build():
    """Running twice on the same file_dict yields byte-identical output.
    Required for reapply, we must not invalidate the cache for a no-op
    fix-loop pass."""
    files = [
        ("public/sw.js", SW_TEMPLATE),
        ("server.js", "// frozen"),
    ]
    once = dict(build._bump_service_worker_cache_version(files, {}))
    twice = dict(build._bump_service_worker_cache_version(
        list(once.items()), {}))
    assert once["public/sw.js"] == twice["public/sw.js"]


def test_reapply_with_changed_content_bumps_cache():
    """When a fix loop CHANGES a file, reapply must bump the cache name
    so stale service workers evict the old shell."""
    base = [
        ("public/sw.js", SW_TEMPLATE),
        ("server.js", "// before"),
    ]
    stamped = dict(build._bump_service_worker_cache_version(base, {}))
    # Mutate server.js (simulating a fix-loop rewrite).
    mutated = [
        ("public/sw.js", stamped["public/sw.js"]),  # carry stamped version
        ("server.js", "// AFTER; fix applied"),
    ]
    bumped = dict(build._bump_service_worker_cache_version(mutated, {}))
    assert bumped["public/sw.js"] != stamped["public/sw.js"]


def test_handles_double_quoted_cache_name_literal():
    sw_dq = SW_TEMPLATE.replace("'app-shell-v1'", '"app-shell-v1"')
    files = [
        ("public/sw.js", sw_dq),
        ("server.js", "// build"),
    ]
    out = dict(build._bump_service_worker_cache_version(files, {}))
    assert "app-shell-v1" not in out["public/sw.js"]
    import re
    assert re.search(r"CACHE_NAME\s*=\s*'app-shell-[0-9a-f]{12}'", out["public/sw.js"])


# ---------------------------------------------------------------------------
# Pipeline registration + ordering
# ---------------------------------------------------------------------------


def test_bump_processor_registered_for_build_and_reapply():
    import pipeline
    assert "_bump_service_worker_cache_version" in pipeline.REGISTRY
    proc = pipeline.REGISTRY["_bump_service_worker_cache_version"]
    assert "build" in proc.phase
    assert "reapply" in proc.phase


def test_bump_in_expected_processors():
    import pipeline
    assert "_bump_service_worker_cache_version" in pipeline.EXPECTED_PROCESSORS


def test_bump_runs_after_sitemap_emitter():
    """Ordering: bumper runs LATE so its content-hash reflects the final
    shape of the build (after sitemap.xml, favicon.ico, etc. are present)."""
    import pipeline
    order = pipeline.pipeline_order("build")
    sitemap_pos = order.index("_ensure_sitemap_xml")
    bump_pos = order.index("_bump_service_worker_cache_version")
    assert sitemap_pos < bump_pos
