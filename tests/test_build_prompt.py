"""Tests for cli/build.py::_build_explicit_prompt.

Section 06 of the B+C pipeline rewrite (section 5 of
docs/plans/b-plus-c/claude-plan.md; see "Plan section labels" in cli/README.md).
The generation-pass prompt was pruned
to stop describing the three admin surfaces that Python widgets now
emit. The contract surface is:

- Admin-markup instruction headers are absent from the prompt body.
- The replacement directive "Do NOT generate these files" is present.
- The HARD UX RULES section is retained for non-form surfaces (layout,
  home, login, etc.) that Claude still writes.
- The total prompt size (character-count proxy) stays at least 8% below
  the reconstructed pre-pruning baseline pinned in
  tests/fixtures/v19_prompt_token_count.json.
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import re
import sys
from pathlib import Path

import pytest

# Allow importing from cli/ without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


FIXTURES_DIR = Path(__file__).parent / "fixtures"
V19_RAW = FIXTURES_DIR / "claude_mw_v19_raw.json"
V19_TOKEN_COUNT = FIXTURES_DIR / "v19_prompt_token_count.json"


def _load_v19_spec() -> dict:
    return json.loads(V19_RAW.read_text())["spec"]


def _build_prompt() -> str:
    return build._build_explicit_prompt(_load_v19_spec())


# ---------------------------------------------------------------------------
# Removed-instruction-block assertions
# ---------------------------------------------------------------------------


def test_prompt_does_not_contain_admin_settings_markup_header():
    """The old '## views/admin/settings.ejs' instruction header is gone.

    Section-06 moves that surface out of the Claude prompt; the Python
    widget registry emits it instead. The remaining mention of the file
    path inside the replacement 'Do NOT generate these files' directive
    is fine - this assertion targets the old section header format only.
    """
    prompt = _build_prompt()
    assert "## views/admin/settings.ejs" not in prompt
    # Guard variants of the same header pattern.
    assert not re.search(r"(?m)^##\s+views/admin/settings\.ejs", prompt)


def test_prompt_does_not_describe_admin_crud_form_structure():
    """Prompt no longer contains the admin CRUD new.ejs / edit.ejs form
    structure guidance (form action URL, CSRF placement, field-type
    mapping)."""
    prompt = _build_prompt()
    # Old markup guidance anchored on concrete directive phrases.
    assert "## views/admin/<slug>/new.ejs" not in prompt
    assert "## views/admin/<slug>/edit.ejs" not in prompt
    # Bare 'form action="/<slug>"' directive is the shape widgets emit
    # internally; the prompt must not instruct Claude to emit it.
    assert 'form action="/<slug>"' not in prompt


def test_prompt_does_not_describe_dashboard_settings_section_markup():
    """Prompt no longer contains the dashboard settings CTA vs inline-form
    guidance that the widget pipeline now owns.

    Section-06 intent: remove guidance on whether dashboard should carry
    an inline settings form or a CTA link. The widget pipeline injects
    the CTA snippet at build time via _inject_dashboard_settings_section.
    """
    prompt = _build_prompt()
    assert "## Dashboard settings section markup" not in prompt
    # Inline settings-form guidance phrases are gone.
    assert "Do NOT embed a second\n   settings form inside the admin dashboard" not in prompt


# ---------------------------------------------------------------------------
# Retained-content assertions
# ---------------------------------------------------------------------------


def test_prompt_retains_hard_ux_rules_section_header():
    """The HARD UX RULES heading remains so Claude still gets non-form
    surface guidance (layout, home, login, etc.)."""
    prompt = _build_prompt()
    assert "HARD UX RULES" in prompt


def test_prompt_contains_do_not_generate_directive():
    """The replacement directive substring MUST be present.

    The exact phrasing varies slightly across plan revisions, but the
    literal 'Do NOT generate these files' substring is the load-bearing
    anchor that downstream docs and future tests grep against.
    """
    prompt = _build_prompt()
    assert "Do NOT generate these files" in prompt


# ---------------------------------------------------------------------------
# Token-count regression guard
# ---------------------------------------------------------------------------


# The pinned baseline is 44,790 characters. The prompt measured 40,286
# characters (a 10.06% drop) when the repository was archived, so a floor of
# 10% sat 0.06 points from failing and tripped on any small prompt edit. The
# floor below leaves about 900 characters of headroom while still failing if
# the removed admin-markup instruction blocks creep back into the prompt.
_MIN_PROMPT_DROP_RATIO = 0.08


def test_prompt_size_stays_at_least_8_percent_below_v19_baseline():
    """Character-count proxy for the prompt's size stays well under the
    reconstructed pre-pruning baseline.

    Approach (b) per the section-06 implementation plan: the baseline is
    the pre-prune v19 prompt body PLUS the three admin-markup instruction
    blocks that §7.1 specified were being removed. The JSON fixture pins
    the number so the threshold survives incidental prompt churn.
    """
    baseline = json.loads(V19_TOKEN_COUNT.read_text())
    baseline_count = int(baseline["token_count"])
    assert baseline_count > 0

    prompt = _build_prompt()
    current_count = len(prompt)
    assert current_count > 0

    drop_ratio = (baseline_count - current_count) / baseline_count
    assert drop_ratio >= _MIN_PROMPT_DROP_RATIO, (
        f"prompt size drop only {drop_ratio:.2%}; "
        f"expected >={_MIN_PROMPT_DROP_RATIO:.0%} vs baseline {baseline_count} "
        f"(current size {current_count})"
    )
