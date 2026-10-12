"""web/public/style.css is committed build output: check it is not stale.

The file is what `npm run css:build` writes (Tailwind, minified). It is
committed because two things need it without a build step: `npm run dev`, which
starts the server from the TypeScript source and never runs the CSS build, and
the web unit tests, which request /style.css from the app. A committed build
artifact goes stale silently when a view gains a class and nobody rebuilds, so
this test runs the same build into a scratch file and compares the bytes.

Needs `cd web && npm ci` (it skips without web/node_modules) and node on PATH.
The build makes no network call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
COMMITTED = WEB / "public" / "style.css"
TAILWIND = WEB / "node_modules" / ".bin" / "tailwindcss"
CSS_BUILD = "tailwindcss -i src/styles/input.css -o public/style.css --minify"


def _scripts() -> dict:
    return json.loads((WEB / "package.json").read_text())["scripts"]


def test_the_stylesheet_is_committed_because_dev_and_tests_serve_it_unbuilt():
    scripts = _scripts()
    assert scripts["css:build"] == CSS_BUILD, "the comparison below runs this command; update both together"
    assert "npm run css:build" in scripts["build"]
    assert "css" not in scripts["dev"], "`npm run dev` now builds the CSS: the committed copy may no longer be needed"
    assert COMMITTED.is_file()


@pytest.mark.requires_tools("node")
def test_the_committed_stylesheet_is_what_the_build_produces(tmp_path):
    if not TAILWIND.exists():
        pytest.skip("needs web/node_modules: run `cd web && npm ci`")
    fresh = tmp_path / "style.css"
    # CSS_BUILD with another output path, run where package.json runs it
    result = subprocess.run([str(TAILWIND), "-i", "src/styles/input.css", "-o", str(fresh), "--minify"],
                            cwd=WEB, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert fresh.read_bytes() == COMMITTED.read_bytes(), (
        "web/public/style.css is not what the build produces from the current views and styles: "
        "run `cd web && npm run css:build` and commit the result"
    )
