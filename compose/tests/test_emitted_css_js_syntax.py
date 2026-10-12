"""Every stylesheet and every script a build emits is syntactically valid.

The engine keeps CSS and JavaScript in Python string literals, some of them
f-strings (a literal brace is written `{{`) and some plain strings (a brace
is written once). A segment written in the wrong regime does not fail the
build: it emits `}}`, the browser's CSS parser recovers by dropping the rule
that follows, and a test that looks for a substring still finds it. That
happened in the editorial-sanctuary layout.

So this module does not look for substrings. It builds

  - every shipped fixture (the v2 samples, and the v1 samples through the
    translator),
  - one spec per archetype under every template family (which is what
    selects the flourish set and the typography rules), and once more with
    the readability option,
  - every page layout variant of every archetype,

and for every emitted `.css` file and every `<style>` block of every
emitted view it requires: balanced braces, no `{{` and no `}}` anywhere (the
emitted CSS closes nested blocks as `} }`, so two adjacent braces are always
a mistake), and, when web/node_modules has postcss (it does after
`cd web && npm ci`; Tailwind brings it), a real parse in which every rule
has a selector and every declaration a property.

JavaScript gets the same treatment: every emitted `.js` file passes
`node --check`, and every inline `<script>` block of every emitted view
compiles.

Views are EJS, so a `<style>` or `<script>` block can contain template tags.
They are replaced before the check: an output tag (`<%= x %>`, `<%- x %>`)
by a neutral token, a scriptlet (`<% ... %>`) by nothing. Braces inside the
tags are template code, not CSS or JS.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, compose_files, resolve_spec
from compose.intake import translate_v1_spec

COMPOSE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = COMPOSE_DIR.parent
FIXTURES = COMPOSE_DIR / "fixtures"
WEB_MODULES = REPO_ROOT / "web" / "node_modules"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")
needs_postcss = pytest.mark.skipif(
    NODE is None or not (WEB_MODULES / "postcss").exists(),
    reason="needs node and web/node_modules/postcss (run `cd web && npm ci`)",
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")


# ---------------------------------------------------------------------------
# The builds
# ---------------------------------------------------------------------------

def _fixture_specs() -> list:
    """(label, v2 spec) for every shipped fixture."""
    specs = []
    for path in sorted(FIXTURES.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if path.name.startswith("v1-"):
            raw = translate_v1_spec(raw)
        specs.append((path.stem, raw))
    return specs


def _one_spec_per_archetype() -> dict:
    """archetype id -> a spec of that archetype (a shipped fixture when one
    exists, otherwise the coach sample with the archetype swapped in)."""
    by_archetype = {}
    for _label, spec in _fixture_specs():
        by_archetype.setdefault(spec["archetype"], spec)
    for archetype in B.load_archetypes()["archetypes"]:
        if archetype["id"] not in by_archetype:
            bare = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
            bare.update(archetype=archetype["id"], palette="", font_pair="", pages={}, seed_content={})
            by_archetype[archetype["id"]] = bare
    return by_archetype


def _compose(spec: dict, layouts: dict = None) -> dict:
    rs = resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
    for page_id, layout_id in (layouts or {}).items():
        rs.page_layouts[page_id] = B.get_layout(layout_id)
    return dict(compose_files(rs))


def _layout_variants() -> list:
    """(archetype id, page id, layout id) for every layout that shares a
    variant_group with the default layout of one of the archetype's pages.
    The default itself is included."""
    layouts = B.load_layouts()["layouts"]
    out = []
    for archetype in B.load_archetypes()["archetypes"]:
        for page_id, default_id in archetype["page_layouts"].items():
            group = layouts[default_id].get("variant_group")
            siblings = sorted(lid for lid, layout in layouts.items()
                              if group and layout.get("variant_group") == group) or [default_id]
            out.extend((archetype["id"], page_id, layout_id) for layout_id in siblings)
    return out


def _all_builds():
    """Yield (label, {path: content}) for the whole matrix."""
    for label, spec in _fixture_specs():
        yield label, _compose(spec)
    families = [t["id"] for t in B.load_templates()["templates"]]
    per_archetype = _one_spec_per_archetype()
    for archetype_id, spec in sorted(per_archetype.items()):
        for family in families:
            yield f"{archetype_id} family={family}", _compose(dict(spec, template_family_id=family))
        yield f"{archetype_id} readability", _compose(dict(spec, readability_bump=True))
    for archetype_id, page_id, layout_id in _layout_variants():
        yield f"{archetype_id} {page_id}={layout_id}", _compose(per_archetype[archetype_id], {page_id: layout_id})


# ---------------------------------------------------------------------------
# Pulling CSS and JS out of the emitted files
# ---------------------------------------------------------------------------

_EJS_TAG_RE = re.compile(r"<%([=\-_#]?)(.*?)[-_]?%>", re.DOTALL)
# The attributes of an opening tag. A template tag is taken whole, so the
# `>` that ends `<% if (cspNonce) { %>` inside `<script ...>` is not read as
# the end of the opening tag (the emitted views write the nonce that way).
_TAG_ATTRS = r"((?:<%.*?%>|\"[^\"]*\"|'[^']*'|[^>\"'])*)"
_STYLE_BLOCK_RE = re.compile(r"<style\b" + _TAG_ATTRS + r">(.*?)</style>", re.DOTALL | re.IGNORECASE)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b" + _TAG_ATTRS + r">(.*?)</script>", re.DOTALL | re.IGNORECASE)
_EJS_IN_ATTRS_RE = re.compile(r"<%.*?%>", re.DOTALL)


def _without_ejs(text: str, placeholder: str) -> str:
    """Replace output tags by `placeholder` and remove scriptlets and
    comments, keeping line breaks so reported line numbers still match."""
    def swap(tag):
        newlines = "\n" * tag.group(0).count("\n")
        return (placeholder if tag.group(1) in ("=", "-") else "") + newlines
    return _EJS_TAG_RE.sub(swap, text)


def _css_sources(files: dict):
    """Yield (where, css) for every stylesheet and <style> block."""
    for path, body in files.items():
        if not isinstance(body, str):
            continue
        if path.endswith(".css"):
            yield path, body
        elif path.endswith((".ejs", ".html")):
            for number, (_attrs, block) in enumerate(_STYLE_BLOCK_RE.findall(body), 1):
                yield f"{path} <style> #{number}", _without_ejs(block, "x")


def _inline_scripts(files: dict):
    """Yield (where, js) for every inline <script> block that is JavaScript."""
    for path, body in files.items():
        if not isinstance(body, str) or not path.endswith((".ejs", ".html")):
            continue
        for number, (attrs, block) in enumerate(_SCRIPT_BLOCK_RE.findall(body), 1):
            attrs = _EJS_IN_ATTRS_RE.sub("", attrs)
            if re.search(r"\bsrc\s*=", attrs):
                continue
            kind = re.search(r"\btype\s*=\s*[\"']([^\"']*)[\"']", attrs)
            if kind and kind.group(1).lower() not in ("text/javascript", "module", "application/javascript"):
                continue  # JSON-LD and the like are data, not script
            yield f"{path} <script> #{number}", _without_ejs(block, "null")


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_STRING_RE = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'")


def _brace_problems(css: str) -> list:
    """Problems found without a parser: unbalanced braces, and any two
    adjacent braces of the same kind."""
    bare = _CSS_STRING_RE.sub('""', _CSS_COMMENT_RE.sub("", css))
    problems = []
    for doubled in ("{{", "}}"):
        at = bare.find(doubled)
        if at >= 0:
            problems.append(f"`{doubled}` near: ...{bare[max(0, at - 80):at + 40].strip()!r}")
    depth = 0
    for position, char in enumerate(bare):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                problems.append(f"`}}` with no open block near: ...{bare[max(0, position - 80):position + 1]!r}")
                break
    if depth > 0:
        problems.append(f"{depth} block(s) never closed")
    return problems


@pytest.fixture(scope="module")
def emitted():
    """The whole matrix, built once: unique CSS texts, unique inline
    scripts and unique .js files, each with the first place it was seen."""
    css, scripts, js_files, shipped_js = {}, {}, {}, {}
    builds = 0
    shipped_labels = {label for label, _spec in _fixture_specs()}
    for label, files in _all_builds():
        builds += 1
        for where, text in _css_sources(files):
            css.setdefault(text, f"{label}: {where}")
        for where, text in _inline_scripts(files):
            scripts.setdefault(text, f"{label}: {where}")
        for path, body in files.items():
            if isinstance(body, str) and path.endswith(".js"):
                js_files.setdefault(body, f"{label}: {path}")
                if label in shipped_labels:
                    shipped_js.setdefault(body, f"{label}: {path}")
    return {"builds": builds, "css": css, "scripts": scripts, "js_files": js_files, "shipped_js": shipped_js}


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

def test_the_matrix_covers_every_fixture_family_and_layout_variant(emitted):
    fixtures = _fixture_specs()
    assert len(fixtures) == len(list(FIXTURES.glob("*.json"))) >= 13
    families = [t["id"] for t in B.load_templates()["templates"]]
    assert "editorial-sanctuary" in families and len(families) >= 6
    variants = _layout_variants()
    assert len({layout for _a, _p, layout in variants}) >= 30
    archetypes = len(B.load_archetypes()["archetypes"])
    assert emitted["builds"] == len(fixtures) + archetypes * (len(families) + 1) + len(variants)
    # The checks below have something to check.
    assert len(emitted["css"]) >= 10 and len(emitted["scripts"]) >= 3 and len(emitted["js_files"]) >= 50
    # The inline scripts include the ones every site ships: the navigation
    # toggle and the admin list and detail formatters.
    script_homes = " ".join(emitted["scripts"].values())
    assert "views/widgets/shell/nav-" in script_homes and "admin" in script_homes
    assert any("public/style.css" in where for where in emitted["css"].values())
    assert any("layout-public.ejs <style>" in where for where in emitted["css"].values())
    # Every flourish set and both eyebrow styles were emitted at least once.
    all_css = "\n".join(emitted["css"])
    for flourish in B.load_flourish_sets()["sets"]:
        if flourish.get("css"):
            assert f"/* Flourish set: {flourish['id']} */" in all_css, flourish["id"]
    assert ".cas-eyebrow-italic::before" in all_css and "body.cas-readability-bump" in all_css


def test_no_emitted_stylesheet_has_unbalanced_or_doubled_braces(emitted):
    bad = {where: _brace_problems(text) for text, where in emitted["css"].items() if _brace_problems(text)}
    assert not bad, json.dumps(bad, indent=2)[:4000]


def test_the_brace_check_sees_what_it_is_for():
    """The exact shape of the editorial-sanctuary defect, and its cousins."""
    good = ".a { color: red; }\n@media (min-width: 1px) { .b { color: blue; } }\n.c::before { content: '}}'; }"
    assert _brace_problems(good) == []
    assert _brace_problems("/* }} */ .a { color: red; }") == []
    doubled_close = "body.x .eyebrow { gap: 0.625rem; }}\n    body.x .eyebrow::before { flex-shrink: 0; }}"
    assert any("`}}`" in p for p in _brace_problems(doubled_close))
    assert any("`{{`" in p for p in _brace_problems(".a {{ color: red; }"))
    assert any("never closed" in p for p in _brace_problems(".a { color: red;"))
    assert any("no open block" in p for p in _brace_problems(".a { color: red; } }"))
    assert any("`}}`" in p for p in _brace_problems("@media (min-width: 1px) { .b { color: blue; }}"))
    # A template tag inside a style block is not CSS.
    styled = "<style>:root { <% keys.forEach(function(k) { %><%= k %>: <%= v[k] %>;\n<% }); %> }</style>"
    (where, css), = list(_css_sources({"views/x.ejs": styled}))
    assert where == "views/x.ejs <style> #1" and _brace_problems(css) == [] and "x: x;" in css


_POSTCSS_DRIVER = r"""
const postcss = require(process.argv[1] + '/postcss');
const sheets = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const problems = [];
let rules = 0, declarations = 0;
for (const [where, css] of sheets) {
  let root;
  try {
    root = postcss.parse(css);
  } catch (e) {
    problems.push(where + ': ' + (e.reason || e.message) + ' at line ' + e.line);
    continue;
  }
  root.walk((node) => {
    if (node.type === 'rule') {
      rules += 1;
      if (!node.selector || !node.selector.trim()) {
        problems.push(where + ': a block with no selector at line ' + (node.source && node.source.start.line));
      }
    } else if (node.type === 'decl') {
      declarations += 1;
      if (!node.prop || /[{}]/.test(node.prop) || /[{}]/.test(node.value || '')) {
        problems.push(where + ': a declaration with a brace in it at line ' + (node.source && node.source.start.line));
      }
    } else if (node.type === 'atrule' && /[{}]/.test(node.params || '')) {
      problems.push(where + ': an at-rule with a brace in its prelude at line ' + (node.source && node.source.start.line));
    }
  });
}
process.stdout.write(JSON.stringify({ problems, rules, declarations }));
"""


def _postcss(tmp_path, sheets: list) -> dict:
    payload = tmp_path / "sheets.json"
    payload.write_text(json.dumps(sheets), encoding="utf-8")
    proc = subprocess.run([NODE, "-e", _POSTCSS_DRIVER, str(WEB_MODULES), str(payload)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@needs_postcss
def test_every_emitted_stylesheet_parses(emitted, tmp_path):
    result = _postcss(tmp_path, [[where, text] for text, where in emitted["css"].items()])
    assert result["problems"] == [], "\n".join(result["problems"][:40])
    assert result["rules"] > 500 and result["declarations"] > 1000


@needs_postcss
def test_the_parser_rejects_the_defect(tmp_path):
    result = _postcss(tmp_path, [
        ["close twice", "a { color: red; }} b { color: blue; }"],
        ["open twice", "a {{ color: red; } b { color: blue; }"],
        ["both twice", "a {{ color: red; }} b { color: blue; }"],
        ["never closed", "a { color: red; "],
        ["fine", "a { color: red; } @media (min-width: 1px) { b { color: blue; } }"],
    ])
    flagged = sorted({problem.split(":", 1)[0] for problem in result["problems"]})
    assert flagged == ["both twice", "close twice", "never closed", "open twice"]


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

_COMPILE_DRIVER = r"""
const vm = require('vm');
const blocks = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
const problems = [];
for (const [where, code] of blocks) {
  try {
    new vm.Script(code, { filename: where });
  } catch (e) {
    problems.push(where + ': ' + e.name + ': ' + e.message);
  }
}
process.stdout.write(JSON.stringify(problems));
"""


def _compile(tmp_path, blocks: list) -> list:
    payload = tmp_path / "blocks.json"
    payload.write_text(json.dumps(blocks), encoding="utf-8")
    proc = subprocess.run([NODE, "-e", _COMPILE_DRIVER, str(payload)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@needs_node
def test_every_inline_script_of_every_emitted_view_compiles(emitted, tmp_path):
    problems = _compile(tmp_path, [[where, text] for text, where in emitted["scripts"].items()])
    assert problems == [], "\n".join(problems[:40])


@needs_node
def test_the_inline_script_check_sees_a_syntax_error(tmp_path):
    files = {"views/x.ejs": (
        "<script nonce=\"<%= cspNonce %>\">var a = <%- JSON.stringify(items) %>; if (a) { a = 1; }}</script>\n"
        "<script type=\"application/ld+json\"><%- jsonLd %></script>\n"
        "<script src=\"/js/app.js\"></script>\n"
        "<script>var ok = '<%= title %>';</script>\n"
    )}
    blocks = list(_inline_scripts(files))
    assert [where for where, _ in blocks] == ["views/x.ejs <script> #1", "views/x.ejs <script> #4"]
    problems = _compile(tmp_path, [list(b) for b in blocks])
    assert len(problems) == 1 and problems[0].startswith("views/x.ejs <script> #1: SyntaxError")


def test_the_extractors_read_past_a_template_tag_in_the_opening_tag():
    """The emitted views write the nonce as
    `<script<% if (cspNonce) { %> nonce="..."<% } %>>`. The `>` of the first
    `%>` is not the end of the opening tag: taking it for one would hand the
    checker ` nonce="null">` followed by the code, which happens to compile
    (an assignment and a comparison) and would hide a real error."""
    opening = "<% if (typeof cspNonce !== 'undefined' && cspNonce) { %> nonce=\"<%= cspNonce %>\"<% } %>"
    files = {"views/y.ejs": (
        f"<script{opening}>\nvar a = 1;\n</script>\n"
        f"<script type=\"application/ld+json\"{opening}><%- jsonLd %></script>\n"
        f"<style{opening}>\n.a {{ color: red; }}\n</style>\n"
    )}
    assert list(_inline_scripts(files)) == [("views/y.ejs <script> #1", "\nvar a = 1;\n")]
    assert list(_css_sources(files)) == [("views/y.ejs <style> #1", "\n.a { color: red; }\n")]


def test_every_extracted_block_is_code_not_the_remains_of_a_tag(emitted):
    for text, where in emitted["scripts"].items():
        assert not re.match(r"\s*(nonce|type|src|defer|async)\s*=", text), where
        assert "<%" not in text and "%>" not in text, where
    for text, where in emitted["css"].items():
        assert "<%" not in text and "%>" not in text and "<style" not in text, where


@needs_node
def test_every_emitted_js_file_of_every_shipped_fixture_passes_node_check(emitted, tmp_path):
    """`node --check`, one process per distinct file body (the same file
    emitted by several fixtures is checked once)."""
    failures = []
    for body, where in emitted["shipped_js"].items():
        script = tmp_path / (hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] + ".js")
        script.write_text(body, encoding="utf-8")
        proc = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            lines = proc.stderr.strip().splitlines() or ["failed"]
            failures.append(f"{where}: {next((line for line in lines if 'Error' in line), lines[-1])}")
    assert failures == [], "\n".join(failures[:20])
    assert len(emitted["shipped_js"]) >= 50


@needs_node
def test_every_emitted_js_file_of_the_whole_matrix_compiles(emitted, tmp_path):
    """The rest of the matrix (families, layout variants), compiled in one
    process the way node compiles a CommonJS module."""
    # The operator scripts start with a `#!` line, which is only legal as
    # the first line of a file; it is dropped before the wrapper goes on.
    wrapped = [[where, "(function (exports, require, module, __filename, __dirname) {"
                + re.sub(r"\A#![^\n]*", "", body) + "\n})"]
               for body, where in emitted["js_files"].items()]
    problems = _compile(tmp_path, wrapped)
    assert problems == [], "\n".join(problems[:40])
