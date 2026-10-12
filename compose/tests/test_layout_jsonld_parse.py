"""The public layout's JSON-LD must not change how a browser parses the page.

The site description is a spec string, and the layout writes it into the
JSON-LD script element. A description with "<!--<script>" in it used to put
the HTML tokenizer into the script-data double-escaped state, which swallowed
the nav. These tests render the emitted layout, parse the output in headless
Chromium with JavaScript off (through the Playwright package in web/), and
assert on the parsed DOM, not on the text of the page. Without a Chromium
installed for that Playwright package they skip.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from compose.build import BuildSpec, resolve_spec
from compose.tests.test_build_pipeline import _render_emitted_layout

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYWRIGHT_DIR = REPO_ROOT / "web" / "node_modules" / "playwright"
PARSE_HELPER = Path(__file__).resolve().parent / "_parse_page.js"
SAMPLE_SPEC = Path(__file__).resolve().parent.parent / "fixtures" / "sample-healer-spec.json"
BROWSER_UNAVAILABLE = 3
BODY_MARKER = "BODY-MARKER-TEXT"
LINE_SEPARATORS = chr(0x2028) + chr(0x2029)
JSON_LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _render_page(tmp_path: Path, site_description: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    spec = json.loads(SAMPLE_SPEC.read_text(encoding="utf-8"))
    resolved = resolve_spec(BuildSpec.from_dict(spec))
    locals_payload = {
        "settings": {
            "site_name": "Probe Site",
            "site_url": "https://probe.example",
            "site_description": site_description,
            "owner_name": "Probe Owner",
        },
        "nav_items": [{"label": "Home", "href": "/"}, {"label": "About", "href": "/about"}],
        "body": f"<p>{BODY_MARKER}</p>",
        "currentPath": "/",
    }
    rc, out, err = _render_emitted_layout("layout-public.ejs", locals_payload, resolved, tmp_path)
    assert rc == 0, err
    return out


def _parse_in_browser(tmp_path: Path, html: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    if not PLAYWRIGHT_DIR.is_dir():
        pytest.skip("web/node_modules/playwright is not installed (run `cd web && npm ci`)")
    page = tmp_path / "parsed-page.html"
    page.write_text(html, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(PARSE_HELPER), str(PLAYWRIGHT_DIR), str(page)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode == BROWSER_UNAVAILABLE:
        pytest.skip(f"headless Chromium is not installed for Playwright ({proc.stderr.strip()[:200]})")
    assert proc.returncode == 0, proc.stderr[:600]
    return json.loads(proc.stdout)


def _organization_description(ld_text: str) -> str:
    graph = json.loads(ld_text)["@graph"]
    (organization,) = [node for node in graph if node.get("@type") == "Organization"]
    return organization["description"]


def test_a_plain_description_parses_with_the_nav_and_the_body(tmp_path):
    parsed = _parse_in_browser(tmp_path, _render_page(tmp_path, "A plain description."))
    assert parsed["navCount"] >= 1
    assert BODY_MARKER in parsed["bodyText"]
    assert _organization_description(parsed["ldJsonText"]) == "A plain description."


def test_a_description_with_a_comment_and_a_script_tag_keeps_the_nav_and_the_body(tmp_path):
    description = "<!--<script>"
    html = _render_page(tmp_path, description)
    parsed = _parse_in_browser(tmp_path, html)
    assert parsed["navCount"] >= 1, "the nav is missing from the parsed page"
    assert BODY_MARKER in parsed["bodyText"], "the body text is missing from the parsed page"
    assert _organization_description(parsed["ldJsonText"]) == description


def test_closing_tags_and_line_separators_in_the_description_round_trip_through_the_page(tmp_path):
    # With "<" escaped, no closing tag such as "</script " reaches the tokenizer.
    description = "</script ><!--<script>" + LINE_SEPARATORS + "x > y & 'q' \"d\" </"
    html = _render_page(tmp_path, description)
    parsed = _parse_in_browser(tmp_path, html)
    assert parsed["navCount"] >= 1, "the nav is missing from the parsed page"
    assert BODY_MARKER in parsed["bodyText"], "the body text is missing from the parsed page"
    assert _organization_description(parsed["ldJsonText"]) == description


def test_the_json_ld_block_holds_no_angle_bracket_or_line_separator_as_written(tmp_path):
    """The parse tests above are the guard. This states the same rule in the
    source the browser reads: nothing between the script tags can open markup."""
    html = _render_page(tmp_path, "<!--<script></script>" + LINE_SEPARATORS)
    (block,) = JSON_LD_RE.findall(html)
    assert "<" not in block and ">" not in block
    assert not any(char in block for char in LINE_SEPARATORS)
    # The description (three "<", two ">") is in the Organization and the WebSite
    # node, and each "<" and ">" is written as its escape.
    backslash = chr(92)
    assert block.count(backslash + "u003c") == 6
    assert block.count(backslash + "u003e") == 4
