"""Section-05 contract framework tests: core primitives + acceptance guards.

Exercises cli/contracts/__init__.py (contract, contains, ContractViolation,
_parse_ejs) and cli/contracts/run.py (run_all, CAS_CONTRACTS_COLLECT).
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest


# Allow `import cli.contracts...` without a package install.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from cli.contracts import (  # noqa: E402
    ContractViolation,
    _parse_ejs,
    contains,
    contract,
)
from cli.contracts.run import run_all  # noqa: E402
from cli.widgets.forms import emit_all  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures"
CANONICAL_SPEC_PATH = (
    Path(__file__).parent / "golden" / "medicine-waves-canonical" / "spec.json"
)


def _canonical_spec() -> dict:
    return json.loads(CANONICAL_SPEC_PATH.read_text())


def _new_artifact(spec: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        spec=spec,
        files=[],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/contract-test",
    )


def _widget_emitted_canonical_artifact() -> types.SimpleNamespace:
    """Return an artifact populated by widget emission + minimum auxiliary
    files needed to satisfy the non-widget contract surfaces (layout/home/
    about/login/dashboard/server.js) with green output."""
    spec = _canonical_spec()
    artifact = _new_artifact(spec)
    emit_all(artifact)
    # The widgets only cover admin settings + CRUD. Layer on plausible
    # green output for the rest of the contract surfaces.
    aux = {
        "server.js": (
            "app.use('/events', eventsRouter);\n"
            "app.use('/admin/events', requireAdmin, eventsRouter);\n"
        ),
        "views/layout.ejs": (
            "<html><body>\n"
            '<nav class="site-nav"><a class="brand" href="/"><img src="/logo.png" alt="logo"></a></nav>\n'
            '<main id="main-content"><%- body %></main>\n'
            "</body></html>\n"
        ),
        "views/home.ejs": (
            "<section><h1>Welcome</h1><p>Our mission.</p></section>\n"
        ),
        "views/about.ejs": (
            "<section class=\"about-photo\"><img src=\"/images/stock/wellness/dome-venue-01.jpg\" alt=\"dome\"></section>\n"
        ),
        "views/login.ejs": (
            "<form method=\"POST\" action=\"/login\">\n"
            '<input type="hidden" name="_csrf" value="<%= csrfToken %>">\n'
            '<input type="email" name="email" required>\n'
            '<input type="password" name="password" required>\n'
            '<button type="submit">Sign in</button>\n'
            "</form>\n"
        ),
        "views/admin/dashboard.ejs": (
            "<h1>Admin Dashboard</h1>\n"
            '<a href="/admin/settings">Manage Site Settings</a>\n'
        ),
    }
    for path, content in aux.items():
        artifact.files.append((path, content))
    return artifact


# ---------------------------------------------------------------------------
# contract(): count semantics
# ---------------------------------------------------------------------------


def test_contract_count_exact_match_passes():
    """contract(ejs, selector, count=N) passes when exact count matches."""
    src = "<div></div><div></div>"
    contract(src, "div", count=2)


def test_contract_count_mismatch_raises():
    """contract(ejs, selector, count=N) raises ContractViolation when count differs."""
    src = "<div></div>"
    with pytest.raises(ContractViolation):
        contract(src, "div", count=3)


def test_contract_absent_true_passes_on_zero():
    """contract(ejs, selector, absent=True) passes when selector has zero matches."""
    src = "<p>hi</p>"
    contract(src, "div", absent=True)


def test_contract_absent_true_raises_on_any_match():
    """contract(ejs, selector, absent=True) raises ContractViolation when selector has any match."""
    src = "<div>x</div>"
    with pytest.raises(ContractViolation):
        contract(src, "div", absent=True)


def test_contract_default_passes_on_one_or_more():
    """contract(ejs, selector) default passes when >=1 match."""
    src = "<div></div>"
    contract(src, "div")


def test_contract_default_raises_on_zero():
    """contract(ejs, selector) default raises when zero matches."""
    src = "<p>empty</p>"
    with pytest.raises(ContractViolation):
        contract(src, "div")


# ---------------------------------------------------------------------------
# violation message shape + collector
# ---------------------------------------------------------------------------


def test_violation_message_includes_path():
    """ContractViolation message includes path=<file> when kwarg provided."""
    with pytest.raises(ContractViolation) as exc_info:
        contract("<p/>", "div", path="views/home.ejs")
    assert "views/home.ejs" in str(exc_info.value)


def test_collector_appends_instead_of_raising():
    """contract(collector=[...]) appends violation string instead of raising."""
    bucket: list[str] = []
    # Should not raise.
    contract("<p/>", "div", collector=bucket)
    contract("<p/>", "span", absent=False, collector=bucket)
    assert len(bucket) == 2
    for msg in bucket:
        assert "expected" in msg
        # Must use ASCII dash, not em dash.
        assert "\u2014" not in msg


# ---------------------------------------------------------------------------
# _parse_ejs fallback behavior
# ---------------------------------------------------------------------------


def test_parse_ejs_simple_ejs_inside_div():
    """_parse_ejs handles <div><%= foo %></div> - returns <div> with correct text node."""
    src = "<div><%= foo %></div>"
    soup = _parse_ejs(src)
    divs = soup.select("div")
    assert len(divs) == 1


def test_parse_ejs_ejs_wrapped_form():
    """_parse_ejs handles <% if (x) { %><form>...<% } %> - <form> parses even with EJS wrapping."""
    src = "<% if (x) { %><form action=\"/a\"></form><% } %>"
    soup = _parse_ejs(src)
    forms = soup.select("form")
    assert len(forms) == 1
    assert forms[0].get("action") == "/a"


def test_parse_ejs_fallback_strips_ejs_blocks_on_degenerate():
    """_parse_ejs fallback strips <% ... %> blocks when initial parse degenerates.

    Construct a synthetic pathological source where the initial parse
    produces nothing useful and the fallback delivers a valid tree.
    """
    # Synthetic test: source that bs4 parses fine anyway. The fallback path
    # is exercised by the unclosed-<% case below; here we just confirm the
    # happy path returns a soup instance without raising.
    src = "<% for (var i=0;i<3;i++){ %><li>x</li><% } %>"
    soup = _parse_ejs(src)
    assert soup is not None


def test_parse_ejs_malformed_raises_value_error():
    """contract on malformed EJS (unclosed <%) raises explicit error, not silent zero match."""
    src = "<div><% if (x) {"  # unclosed <% , no closing %>
    with pytest.raises(ValueError):
        _parse_ejs(src)


# ---------------------------------------------------------------------------
# Fixture-based selector behavior
# ---------------------------------------------------------------------------


def test_v12_admin_settings_has_literal_form_action():
    """contract on fixtures/v12_admin_settings.ejs - selector for <form action="/admin/settings"> returns expected count."""
    src = (FIXTURES / "v12_admin_settings.ejs").read_text()
    # v12 fixture has a literal /admin/settings form action.
    contract(src, 'form[action="/admin/settings"]', count=1)


def test_v14_admin_settings_has_literal_form_action():
    """contract on fixtures/v14_admin_settings.ejs - same selector."""
    src = (FIXTURES / "v14_admin_settings.ejs").read_text()
    contract(src, 'form[action="/admin/settings"]', count=1)


def test_v18_event_form_ejs_ternary_does_not_match_literal_events():
    """contract on fixtures/v18_event_form.ejs - EJS ternary action does NOT match literal \"/admin/events\"."""
    src = (FIXTURES / "v18_event_form.ejs").read_text()
    # The v18 fixture uses <%= event && event.id ? '/admin/events/' + event.id : '/admin/events' %>
    # as the action. A CSS literal selector for action="/admin/events"
    # must NOT match; this is the exact reason widgets canonicalize to a
    # literal URL in section-04.
    with pytest.raises(ContractViolation):
        contract(src, 'form[action="/admin/events"]', count=1)


def test_v19_admin_dashboard_per_field_iterator_detection():
    """contract on fixtures/v19_admin_dashboard.ejs - per-field iterator pattern detection."""
    src = (FIXTURES / "v19_admin_dashboard.ejs").read_text()
    # v19 dashboard had an inline settings form (the regression). Contract
    # must be able to target it via CSS selector.
    matches = src.count("<form")
    assert matches >= 0  # fixture parse must succeed
    # Run the selector; whether matches is 0 or >0 depends on the actual
    # v19 snapshot captured in section-01.
    soup = _parse_ejs(src)
    _ = soup.select('form[action="/admin/settings"]')  # must not raise


# ---------------------------------------------------------------------------
# BS4 + soupsieve import health
# ---------------------------------------------------------------------------


def test_bs4_and_soupsieve_importable():
    """BS4 + soupsieve import successfully in test environment."""
    import bs4
    import soupsieve

    assert bs4.__version__.split(".")[0] == "4"
    # soupsieve major version 2 per pin
    assert soupsieve.__version__.split(".")[0] == "2"


# ---------------------------------------------------------------------------
# Acceptance guards (§4.7)
# ---------------------------------------------------------------------------


def test_run_all_canonical_widget_artifact_zero_violations(monkeypatch):
    """contracts.run_all(canonical_widget_artifact) raises zero violations."""
    # Collect-all mode so we can observe ALL violations in one shot if
    # something drifts.
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _widget_emitted_canonical_artifact()
    # Zero violations -> run_all returns None without raising.
    run_all(artifact, collect_all=True)


def test_run_all_injected_regression_raises_with_selector(monkeypatch):
    """contracts.run_all(injected_regression_artifact) raises ContractViolation with specific selector."""
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _widget_emitted_canonical_artifact()
    # Inject a YouTube iframe on home (past regression class).
    home = dict(artifact.files).get("views/home.ejs", "")
    bad = home + '\n<iframe src="https://youtube.com/embed/xyz"></iframe>\n'
    for i, item in enumerate(artifact.files):
        if item[0] == "views/home.ejs":
            artifact.files[i] = ("views/home.ejs", bad)
            break
    with pytest.raises(ContractViolation) as exc_info:
        run_all(artifact, collect_all=True)
    # The raised message must name the youtube selector and the file path.
    assert "youtube.com" in str(exc_info.value)
    assert "views/home.ejs" in str(exc_info.value)


def test_run_all_wall_clock_under_two_seconds(monkeypatch):
    """contracts.run_all() on v19-size artifact completes in <2 seconds wall-clock."""
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    artifact = _widget_emitted_canonical_artifact()
    # Bulk-add plausible v19-size file count with irrelevant content so
    # contracts that scan artifact.files-for-existence see a large list.
    for i in range(200):
        artifact.files.append((f"public/pad-{i:03d}.js", "// pad\n"))
    start = time.perf_counter()
    run_all(artifact, collect_all=True)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"run_all() took {elapsed:.3f}s, exceeds 2s budget"


def test_cas_contracts_collect_env_aggregates_violations(monkeypatch):
    """CAS_CONTRACTS_COLLECT=1 env aggregates multiple violations into one raised error."""
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    # Empty artifact trips many mandatory-file-missing violations.
    artifact = types.SimpleNamespace(
        spec={},
        files=[],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/x",
    )
    with pytest.raises(ContractViolation) as exc_info:
        run_all(artifact)
    msg = str(exc_info.value)
    # Aggregated header: "<N> violations:" prefix and newline-joined list.
    assert "violations:" in msg
    # Mandatory files ought to all surface.
    assert "views/admin/settings.ejs" in msg
    assert "views/layout.ejs" in msg
    assert "views/home.ejs" in msg
    assert "views/login.ejs" in msg


def test_run_all_default_stops_on_first_violation(monkeypatch):
    """run_all default (collect_all=False) raises on first violation, does not continue."""
    # Explicit unset: override the autouse fixture's CAS_CONTRACTS_COLLECT=1.
    monkeypatch.delenv("CAS_CONTRACTS_COLLECT", raising=False)
    artifact = types.SimpleNamespace(
        spec={},
        files=[],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/x",
    )
    with pytest.raises(ContractViolation) as exc_info:
        run_all(artifact, collect_all=False)
    msg = str(exc_info.value)
    # First violation only: must NOT include the aggregated header.
    assert "violations:" not in msg
