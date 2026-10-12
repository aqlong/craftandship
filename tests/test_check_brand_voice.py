"""Tests for scripts/check_brand_voice.py (the pre-commit brand-voice lint).

The most important test here runs the lint over EVERY file the pre-commit hook
would select on a first commit. The hook only lints staged files, so a
violation in an old file stays invisible until the day someone touches it (or
until the first commit of a fresh clone stages everything at once). This test
makes the whole-tree state a CI-visible fact.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.check_brand_voice as bv  # noqa: E402

EM_DASH = chr(0x2014)  # built at runtime: no literal em dash may exist in the repo


def _write(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rules(violations) -> set[str]:
    return {rule for _line, _text, rule in violations}


def test_citation_page_may_name_vendors(tmp_path):
    page = _write(tmp_path, "web/views/docs-research.ejs",
                  "<li>Mindbody Business Pricing</li>\n<li>Booker vs Mindbody (GetApp)</li>\n")
    assert bv.scan_file(page) == []


def test_citation_page_exemption_is_competitor_rule_only(tmp_path):
    page = _write(tmp_path, "web/views/docs-research.ejs",
                  f"<li>Mindbody {EM_DASH} pricing on GitHub</li>\n")
    assert _rules(bv.scan_file(page)) == {"em-dash", "tech-ownership"}


def test_other_views_are_still_flagged_for_competitors(tmp_path):
    page = _write(tmp_path, "web/views/pricing.ejs", "<p>Better than Mindbody.</p>\n")
    assert _rules(bv.scan_file(page)) == {"competitor"}
    lookalike = _write(tmp_path, "web/views/not-docs-research.ejs", "<p>Mindbody</p>\n")
    assert _rules(bv.scan_file(lookalike)) == {"competitor"}


def test_allow_marker_still_works(tmp_path):
    page = _write(tmp_path, "web/views/pricing.ejs",
                  "<!-- brand-voice-allow-competitor: quoted testimonial -->\n<p>Mindbody</p>\n")
    assert bv.scan_file(page) == []


def test_avoidance_instruction_is_not_ownership_marketing(tmp_path):
    prompt = _write(tmp_path, "web/src/services/claude.ts",
                    '- Plain English. Avoid jargon like "compose," "self-host."\n'
                    "- Never say GitHub or Dockerfile to the customer.\n")
    assert bv.scan_file(prompt) == []


def test_ownership_marketing_is_still_flagged(tmp_path):
    page = _write(tmp_path, "web/views/home.ejs",
                  "<p>You own your source code and can self-host it.</p>\n")
    assert _rules(bv.scan_file(page)) == {"tech-ownership"}
    # an avoidance cue AFTER the term does not excuse it
    after = _write(tmp_path, "web/views/home2.ejs", "<p>Self-host it; never worry.</p>\n")
    assert _rules(bv.scan_file(after)) == {"tech-ownership"}


def _precommit_copy_scope_regex() -> re.Pattern:
    hook = (REPO_ROOT / ".githooks" / "pre-commit").read_text()
    match = re.search(r"grep -E '(\^\(web/views[^']*)'", hook)
    assert match, "could not find the copy-file scope regex in .githooks/pre-commit"
    return re.compile(match.group(1))


def test_whole_tree_passes_the_precommit_scope():
    scope = _precommit_copy_scope_regex()
    files = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or "node_modules" in path.parts or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if scope.match(rel):
            files.append(rel)
    assert len(files) > 50, "scope regex matched suspiciously few files"
    import os
    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        assert bv.main(["check_brand_voice.py", *files]) == 0, (
            "brand-voice lint fails on the untouched tree: a first `git add -A && git commit` "
            "would be rejected by the pre-commit hook"
        )
    finally:
        os.chdir(cwd)
