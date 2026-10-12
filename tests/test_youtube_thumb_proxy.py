"""Regression tests for the /api/video-thumb/:videoId route injection.

v21 local build surfaced a broken thumbnail display on /videos:
`<img src="/api/video-thumb/X">` was getting JSON (`{thumbnail: URL}`)
instead of image bytes. Root cause: Claude emitted a JSON-returning
handler at the same path, and the pipeline's legacy guard
`if "/api/video-thumb/" not in server_js` treated "path exists" as
"correct implementation exists".

Fix: guard on the handler body signature (the pipeline's canonical
`upstream.pipe(res)` pattern) instead of the path string. If the
signature isn't present, strip Claude's broken handler and inject
the canonical one.

This is the same class of bug as the widget runtime-requirements
issue: pipeline trusted that Claude's emission satisfied an implicit
contract that was never verified. These tests lock the contract.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from build import _ensure_video_embeds  # noqa: E402


def _mk_spec():
    return {
        "app_name": "medicine-waves",
        "description": "Medicine Waves somatic sound journey",
        "data_models": [
            {"name": "Video", "fields": [
                {"name": "title", "type": "text"},
                {"name": "embed_url", "type": "url"},
            ]},
        ],
    }


def test_pipeline_overwrites_claude_json_returning_thumb_handler():
    """When Claude emits a JSON-returning handler at /api/video-thumb/,
    the pipeline must strip it and inject the canonical image-piping
    version. Regression: v21 shipped with Claude's JSON handler intact."""
    claude_broken_server = (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use(express.static(path.join(__dirname, 'public')));\n"
        "app.get('/api/video-thumb/:videoId', (req, res) => {\n"
        "  const videoId = req.params.videoId;\n"
        "  res.json({ thumbnail: 'https://img.youtube.com/vi/' + videoId + '/hqdefault.jpg' });\n"
        "});\n"
        "app.listen(3000);\n"
    )
    files = [
        ("server.js", claude_broken_server),
        ("views/videos/index.ejs",
         '<% videos.forEach(v => { %>\n'
         '<img src="/api/video-thumb/<%= v.youtube_id %>">\n'
         '<% }); %>'),
    ]
    result = dict(_ensure_video_embeds(files, _mk_spec()))
    server_js = result["server.js"]

    # Canonical handler signature must be present
    assert "upstream.pipe(res)" in server_js, (
        "pipeline did not inject canonical image-piping handler"
    )
    # Claude's JSON variant must be gone
    assert "res.json({ thumbnail:" not in server_js, (
        "Claude's JSON-returning handler still present; pipeline failed "
        "to overwrite it"
    )


def test_pipeline_preserves_canonical_handler_when_already_present():
    """Idempotency: a server.js that already has the canonical handler
    is left unchanged (no double-injection)."""
    from cli.build import _ensure_video_embeds
    # Build a server that already has the canonical handler by running
    # the pipeline once.
    initial = (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use(express.static(path.join(__dirname, 'public')));\n"
        "app.listen(3000);\n"
    )
    files = [("server.js", initial),
             ("views/videos/index.ejs", "<img src='/a.jpg'>")]
    pass1 = dict(_ensure_video_embeds(files, _mk_spec()))["server.js"]
    assert "upstream.pipe(res)" in pass1

    # Second pass must not add a duplicate handler
    files2 = [("server.js", pass1),
              ("views/videos/index.ejs", "<img src='/a.jpg'>")]
    pass2 = dict(_ensure_video_embeds(files2, _mk_spec()))["server.js"]
    assert pass1.count("upstream.pipe(res)") == pass2.count("upstream.pipe(res)"), (
        "pipeline added duplicate canonical handler on second pass"
    )


def test_canonical_handler_sets_image_content_type():
    """Sanity: the canonical handler must set Content-Type to image/*,
    NOT application/json. This is what makes <img src> work."""
    files = [
        ("server.js",
         "const express = require('express');\n"
         "const app = express();\n"
         "app.use(express.static(path.join(__dirname, 'public')));\n"),
        ("views/videos/index.ejs", "<img src='/a.jpg'>"),
    ]
    result = dict(_ensure_video_embeds(files, _mk_spec()))
    server_js = result["server.js"]
    assert "image/jpeg" in server_js or "'image/jpeg'" in server_js or \
           "image/svg+xml" in server_js  # fallback mime
    assert "res.json" not in server_js or \
           server_js.count("res.json") == 0, (
        "canonical handler should not call res.json; it pipes bytes"
    )


def test_canonical_handler_validates_video_id_format():
    """The injected handler must reject invalid IDs (non-11-char or
    non-alphanumeric) to prevent path-traversal / open-proxy abuse."""
    files = [
        ("server.js",
         "const express = require('express');\n"
         "const app = express();\n"
         "app.use(express.static(path.join(__dirname, 'public')));\n"),
        ("views/videos/index.ejs", "<img src='/a.jpg'>"),
    ]
    result = dict(_ensure_video_embeds(files, _mk_spec()))
    server_js = result["server.js"]
    # Validates shape: 11-character [a-zA-Z0-9_-] (YouTube video ID).
    assert "/^[a-zA-Z0-9_-]{11}$/" in server_js or \
           "a-zA-Z0-9_-]{11}" in server_js, (
        "canonical handler missing video-ID format validation"
    )
