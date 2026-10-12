"""Safety-net integration tests for the widget-emission path.

Section 06 of the B+C pipeline rewrite (section 5 of
docs/plans/b-plus-c/claude-plan.md; see "Plan section labels" in cli/README.md).
These tests anchor the safety-net claim:

  1. Widgets-ON path: emit_all + contracts.run_all on canonical MW spec
     produces a contracts-green artifact.
  2. Widgets-OFF path: FIX B/L/M/N applied to the recorded v19 Claude
     fixture produces a contracts-green artifact. (See escalation note
     below for the current status on v19 output.)
  3. Byte-identical no-op proof for each of the five safety-net FIX
     processors on widget-emitted markup.

Escalation note (widgets-OFF, v19 fixture):
  tests/fixtures/claude_mw_v19_raw.json captures pre-post-processor
  Claude output that lacks views/admin/<slug>/new.ejs and edit.ejs for
  every writable data model - FIX B/L/M/N cannot create files that never
  existed. The contract layer flags the missing files as violations, so
  the widgets-OFF-on-v19 end-to-end pass is NOT currently achievable
  without either (a) weakening contracts (not acceptable) or (b)
  regenerating the fixture from a pipeline run that emits those files. The
  widgets-OFF test is marked xfail with a strict=True reason pointing at
  this condition, so that a decision to regenerate the fixture, add FIX
  processors that create the missing admin files, or adjust the contract
  surface is made deliberately.

The widgets-ON path on the canonical MW spec remains the primary
live safety-net guarantee until the widgets-OFF fixture is refreshed.
"""

from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

# Allow importing from cli/ without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402

from cli.contracts.run import run_all  # noqa: E402
from cli.widgets.forms import emit_all  # noqa: E402


GOLDEN_DIR = Path(__file__).parent / "golden" / "medicine-waves-canonical"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
V19_RAW = FIXTURES_DIR / "claude_mw_v19_raw.json"
V19_SHA256 = FIXTURES_DIR / "claude_mw_v19_raw.sha256"


def _canonical_spec() -> dict:
    return json.loads((GOLDEN_DIR / "spec.json").read_text())


def _v19_payload() -> dict:
    return json.loads(V19_RAW.read_text())


def _make_artifact(spec: dict, files_items: list) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        spec=spec,
        files=list(files_items),
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/cas-safety-net-test",
    )


# ---------------------------------------------------------------------------
# Widgets-enabled path: widget emission + contracts green on canonical MW.
# ---------------------------------------------------------------------------


def test_pipeline_with_widgets_enabled_on_canonical_spec_passes_contracts(
    monkeypatch,
):
    """emit_all on the canonical MW spec yields a contracts-green artifact.

    Forces WIDGETS_ENABLED=true (bypasses archetype gate). emit_all writes
    views/admin/settings.ejs + views/admin/<slug>/new.ejs + edit.ejs for
    each writable model. We then splice in the minimum non-admin files
    that contracts.run_all requires (layout/home/login/about/dashboard)
    from the recorded v19 fixture so contracts can inspect them. With
    widgets ON, contracts.run_all(artifact, collect_all=True) must pass.
    """
    monkeypatch.setenv("WIDGETS_ENABLED", "true")

    spec = _canonical_spec()
    # Start from the v19 recorded files so non-admin surfaces (layout,
    # home, login, about, admin-dashboard) are populated.
    v19 = _v19_payload()
    files_items = list(v19["files"].items())

    artifact = _make_artifact(spec, files_items)
    # Step 1: widget emission writes the admin files.
    emit_all(artifact)

    # Step 2: the admin_dashboard contract expects views/admin/dashboard.ejs
    # (slash path) OR returns silently if absent; v19 emits the flat
    # views/admin-dashboard.ejs. For the widgets-ON path the dashboard CTA
    # snippet is stored in artifact.widgets_snippets and injected at runtime
    # via _inject_dashboard_settings_section. Splice it manually here so
    # the widgets-ON path does NOT rely on the full pipeline running (the
    # plan's §7.3 factoring: widget emission + contracts is the anchor).
    snippet = artifact.widgets_snippets.get("dashboard_settings_section", "")
    file_dict = dict(artifact.files)
    # The contracts.admin_dashboard module early-returns when
    # views/admin/dashboard.ejs is missing. Emit a minimal conforming
    # dashboard that carries the widget-supplied CTA snippet inside.
    if "views/admin/dashboard.ejs" not in file_dict:
        file_dict["views/admin/dashboard.ejs"] = (
            "<h1>Admin Dashboard</h1>\n"
            "<!-- widgets:dashboard-settings -->\n"
            f"{snippet}\n"
            "<!-- /widgets:dashboard-settings -->\n"
        )
        artifact.files = list(file_dict.items())

    # Step 3: run every contract. collect_all surfaces all violations.
    run_all(artifact, collect_all=True)


# ---------------------------------------------------------------------------
# Widgets-disabled path: FIX B/L/M/N on recorded v19 output.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Known gap: the recorded v19 Claude fixture "
        "(tests/fixtures/claude_mw_v19_raw.json) does not contain "
        "views/admin/<slug>/new.ejs or edit.ejs for its writable data models. "
        "With widgets disabled, the FIX B/L/M/N processors cannot create files "
        "that were never emitted, so contracts.admin_crud reports them "
        "missing. Regenerating the fixture from a post-processor run, or "
        "adding a processor that seeds the missing admin CRUD files, would "
        "make this pass; weakening the contracts to hide it is not an option."
    ),
    strict=True,
)
def test_pipeline_with_widgets_disabled_on_claude_fixture_passes_contracts(
    monkeypatch,
):
    """Widgets-OFF + FIX B/L/M/N on v19 fixture yields contracts-green.

    Current status: xfail - see module docstring and decorator reason.
    """
    monkeypatch.setenv("WIDGETS_ENABLED", "false")
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")

    v19 = _v19_payload()
    spec = v19["spec"]
    files_list = list(v19["files"].items())

    # Apply the FIX safety-net processors in dependency order.
    files_list = build._ensure_crud_forms_complete(files_list, spec)
    files_list = build._strip_inline_dashboard_settings_form(files_list, spec)
    files_list = build._humanize_settings_labels(files_list, spec)
    files_list = build._ensure_upload_form_ui(files_list, spec)
    files_list = build._fix_admin_crud_link_prefix(files_list, spec)

    artifact = _make_artifact(spec, files_list)
    run_all(artifact, collect_all=True)


# ---------------------------------------------------------------------------
# Fixture pin: sha256 guard against silent regeneration.
# ---------------------------------------------------------------------------


def test_claude_mw_v19_raw_fixture_sha256_matches_sidecar():
    """Ensure the v19 fixture body matches the pinned sha256 sidecar.

    If someone regenerates claude_mw_v19_raw.json silently, the safety-net
    claim shifts under our feet. The sidecar file pins the content; this
    test fails the moment a regeneration slips in without a matching
    sidecar update.
    """
    body = V19_RAW.read_bytes()
    expected = V19_SHA256.read_text().strip()
    actual = hashlib.sha256(body).hexdigest()
    assert actual == expected, (
        f"fixture sha256 mismatch: expected {expected}, got {actual} - "
        "regenerate tests/fixtures/claude_mw_v19_raw.sha256 if the "
        "regeneration was intentional"
    )


# ---------------------------------------------------------------------------
# Byte-identical no-op proofs for each safety-net FIX.
# ---------------------------------------------------------------------------


@pytest.fixture
def widget_artifact(monkeypatch):
    """Produce a widgets-ON artifact on the canonical MW spec.

    Each byte-identical no-op test mutates nothing permanent; the fixture
    rebuilds the artifact for every test function so one failure cannot
    contaminate another.
    """
    monkeypatch.setenv("WIDGETS_ENABLED", "true")
    spec = _canonical_spec()
    artifact = _make_artifact(spec, [])
    emit_all(artifact)
    return artifact


def _snapshot_widget_files(artifact) -> dict[str, str]:
    """Return an in-order copy of the admin files emitted by widgets.

    Only admin-scoped paths matter for the byte-identical proofs; the
    artifact has no non-admin files when widgets are the only emitter.
    """
    return {
        path: content
        for path, content in list(artifact.files)
        if path.startswith("views/admin/")
    }


def test_ensure_crud_forms_complete_is_byte_identical_on_widget_output(widget_artifact):
    """FIX B leaves widget CRUD new/edit markup untouched.

    The widget output already carries <form action="/admin/<slug>"> with
    a CSRF hidden input and the canonical widget-emitted fields; FIX B's
    stub-detection predicates (missing <form>, "Content for X will
    appear here.") never trigger, so the file_dict returned must be
    byte-identical to the input.
    """
    before = _snapshot_widget_files(widget_artifact)
    files_after = build._ensure_crud_forms_complete(
        list(widget_artifact.files), widget_artifact.spec,
    )
    after = {p: c for p, c in files_after if p.startswith("views/admin/")}
    assert before == after, (
        "FIX B regression: _ensure_crud_forms_complete mutated widget "
        "output - the safety-net claim depends on byte-identity here"
    )


def test_strip_inline_dashboard_settings_form_is_byte_identical_on_widget_output(widget_artifact):
    """FIX L leaves widget output untouched.

    Widgets never emit an inline <form action="/admin/settings"> inside
    views/admin/dashboard.ejs - the snippet is a CTA link only, stored
    in artifact.widgets_snippets. FIX L's scanner finds no matching
    inline form and returns the input list unchanged.
    """
    before = _snapshot_widget_files(widget_artifact)
    files_after = build._strip_inline_dashboard_settings_form(
        list(widget_artifact.files), widget_artifact.spec,
    )
    after = {p: c for p, c in files_after if p.startswith("views/admin/")}
    assert before == after


def test_humanize_settings_labels_is_byte_identical_on_widget_output(widget_artifact):
    """FIX M leaves widget output untouched.

    Widget emission routes every settings field through humanize() so
    labels are already plain English (e.g. "Hero Tagline", not
    "hero_tagline"). FIX M's snake_case detector finds nothing to
    rewrite and returns the input list unchanged.
    """
    before = _snapshot_widget_files(widget_artifact)
    files_after = build._humanize_settings_labels(
        list(widget_artifact.files), widget_artifact.spec,
    )
    after = {p: c for p, c in files_after if p.startswith("views/admin/")}
    assert before == after


def test_ensure_upload_form_ui_is_byte_identical_on_widget_output(widget_artifact):
    """FIX N leaves widget output untouched.

    Widgets emit ImageUrlWidget output with the canonical drag-drop zone
    shape that FIX N targets. FIX N's scanner detects the zone is
    already present and returns the input list unchanged.
    """
    before = _snapshot_widget_files(widget_artifact)
    files_after = build._ensure_upload_form_ui(
        list(widget_artifact.files), widget_artifact.spec,
    )
    after = {p: c for p, c in files_after if p.startswith("views/admin/")}
    assert before == after


def test_fix_admin_crud_link_prefix_is_byte_identical_on_widget_output(widget_artifact):
    """FIX C leaves widget output untouched.

    Resolved via option (a) from the section-06 task brief: widgets emit
    `<!-- widgets:admin-form -->` at the top of settings.ejs + per-model
    new.ejs / edit.ejs, and _fix_admin_crud_link_prefix now skips any
    .ejs file carrying that marker. FIX C continues to rewrite Claude-
    emitted admin list views that legitimately need the /admin/ prefix
    stripped (v13 regression class), but widget-owned surfaces survive
    unchanged so contracts/admin_crud.py literal /admin/<slug> assertion
    holds.
    """
    before = _snapshot_widget_files(widget_artifact)
    files_after = build._fix_admin_crud_link_prefix(
        list(widget_artifact.files), widget_artifact.spec,
    )
    after = {p: c for p, c in files_after if p.startswith("views/admin/")}
    assert before == after
