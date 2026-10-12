"""Tests for _ensure_favicon_ico_fallback (generated-apps-favicon-ico-fallback).

Generated apps serve public/favicon.svg but
/favicon.ico returned 404, breaking old browsers and some RSS feed
readers. Post-processor copies a bundled default .ico into public/
when an SVG is present but no ICO exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


def test_default_favicon_ico_asset_is_bundled():
    """cli/assets/default_favicon.ico must exist and be a valid ICO file
    (magic bytes: 00 00 01 00 = ICO type 1, plus a non-zero image count)."""
    asset = build._DEFAULT_FAVICON_ICO_PATH
    assert asset.exists(), f"bundled default favicon missing at {asset}"
    data = asset.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00", (
        "default_favicon.ico magic bytes wrong; file is not a valid ICO"
    )
    count = int.from_bytes(data[4:6], "little")
    assert count >= 1, "ICO header claims zero images"
    # Size sanity: a multi-size PNG-encoded ICO is ~1-2KB, not bytes.
    assert len(data) > 200


def test_ensure_favicon_ico_emits_when_svg_present_and_ico_missing():
    """Happy path: SVG is in file_dict, ICO is NOT → post-processor
    injects public/favicon.ico as bytes."""
    files = [
        ("public/favicon.svg", "<svg>whatever</svg>"),
        ("server.js", "// unrelated"),
    ]
    out = dict(build._ensure_favicon_ico_fallback(files))
    assert "public/favicon.ico" in out
    ico = out["public/favicon.ico"]
    assert isinstance(ico, (bytes, bytearray))
    assert ico[:4] == b"\x00\x00\x01\x00"
    # SVG was NOT modified.
    assert out["public/favicon.svg"] == "<svg>whatever</svg>"


def test_ensure_favicon_ico_noop_when_ico_already_present():
    """If public/favicon.ico is already in the file_dict (Claude emitted
    one or a prior reapply added one), do NOT overwrite."""
    existing_ico = b"existing-ico-bytes"
    files = [
        ("public/favicon.svg", "<svg/>"),
        ("public/favicon.ico", existing_ico),
    ]
    out = dict(build._ensure_favicon_ico_fallback(files))
    assert out["public/favicon.ico"] == existing_ico, (
        "post-processor clobbered an existing favicon.ico"
    )


def test_ensure_favicon_ico_noop_when_no_svg():
    """If there is no favicon.svg, do NOT inject an .ico, the app
    simply has no icon (and silently adding only an .ico would mislead
    browsers searching for the canonical SVG via <link rel=icon>)."""
    files = [("server.js", "// no favicons here")]
    out = dict(build._ensure_favicon_ico_fallback(files))
    assert "public/favicon.ico" not in out
    assert "public/favicon.svg" not in out


def test_ensure_favicon_ico_idempotent_on_reapply():
    """Two passes must yield byte-identical output (reapply safety)."""
    files = [("public/favicon.svg", "<svg/>")]
    once = dict(build._ensure_favicon_ico_fallback(files))
    twice = dict(build._ensure_favicon_ico_fallback(list(once.items())))
    assert once == twice


def test_ensure_favicon_ico_registered_for_build_and_reapply():
    """Processor runs in both phases so fix loops can't strip the bundle."""
    import pipeline
    assert "_ensure_favicon_ico_fallback" in pipeline.REGISTRY
    proc = pipeline.REGISTRY["_ensure_favicon_ico_fallback"]
    assert "build" in proc.phase
    assert "reapply" in proc.phase


def test_ensure_favicon_ico_in_expected_processors():
    import pipeline
    assert "_ensure_favicon_ico_fallback" in pipeline.EXPECTED_PROCESSORS


def test_ensure_favicon_ico_runs_after_icon_helper():
    """Ordering: runs after _ensure_icon_helper so icon_helper writes
    its own output first. Both touch the app's icon asset surface."""
    import pipeline
    order = pipeline.pipeline_order("build")
    helper_pos = order.index("_ensure_icon_helper")
    ico_pos = order.index("_ensure_favicon_ico_fallback")
    assert helper_pos < ico_pos
