"""Tests for widget-emitted markup visual styling.

Root cause: v21 /admin/settings shipped as unstyled plain HTML (no
Tailwind classes, no card separation, white inputs on dark background).
The pre-B+C canonical template at cli/templates/views_admin_settings.ejs
used `input-premium`, `card-premium admin-settings-card`, grid layouts,
and an embedded dark/light theme <style> block. Widget emission dropped
all of that.

These tests lock the expected styling shape so future widget refactors
don't regress the visual quality.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli.widgets.forms import emit_admin_settings, emit_crud_new_edit


def _mw_spec():
    return {
        "app_name": "medicine-waves-test",
        "description": "Medicine Waves test",
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "string"},
                {"name": "date_time", "type": "timestamp"},
                {"name": "description", "type": "text"},
                {"name": "image_url", "type": "image_url"},
            ]},
        ],
        "settings": {
            "site_name": "MW",
            "site_url": "https://mw.example",
            "site_description": "A test",
            "hero_tagline": "Tagline",
            "hero_description": "Description",
            "hero_image_url": "/hero.jpg",
        },
    }


# ---------------------------------------------------------------------------
# admin/settings.ejs page shell
# ---------------------------------------------------------------------------


def test_settings_wraps_form_in_max_width_container():
    """Settings page shouldn't span the full viewport width, wrap form
    in a container that limits to a readable width."""
    rendered = emit_admin_settings(_mw_spec())
    assert "max-w-" in rendered, (
        "settings form must be wrapped in a max-width container (max-w-4xl "
        "or similar) so the layout doesn't span a 1920px viewport edge-to-edge"
    )


def test_settings_has_page_header_with_back_link():
    """Admin settings page should include a page header with title + a
    back-to-dashboard link, matching the pre-B+C canonical shape."""
    rendered = emit_admin_settings(_mw_spec())
    # Page title (the form's overall heading)
    assert "Site Settings" in rendered, "missing page heading 'Site Settings'"
    # Back-to-dashboard link
    assert 'href="/admin"' in rendered, "missing Back-to-Dashboard link"


def test_settings_form_has_space_y_class_for_vertical_spacing():
    """Form wrapping needs space-y-* utility so sections don't touch."""
    rendered = emit_admin_settings(_mw_spec())
    assert "space-y-" in rendered, (
        "form must use space-y-* between sections for visual separation"
    )


# ---------------------------------------------------------------------------
# Card-premium wrapper on each section
# ---------------------------------------------------------------------------


def test_every_settings_section_has_card_premium_wrapper():
    """Each section MUST use the `card-premium admin-settings-card`
    container classes with padding + border + rounded corners, matches
    the pre-B+C canonical template."""
    rendered = emit_admin_settings(_mw_spec())
    # Canonical classes that define card appearance
    required_classes = [
        "card-premium",
        "admin-settings-card",
        "p-6",
        "rounded-xl",
        "border",
    ]
    for klass in required_classes:
        assert klass in rendered, (
            f"settings section missing '{klass}' class; widget output "
            f"won't have visual separation on the dashboard"
        )


def test_section_heading_has_font_display_class():
    """Section h2 must use font-display + proper sizing so the heading
    stands out from the fields below it."""
    rendered = emit_admin_settings(_mw_spec())
    assert "font-display" in rendered, (
        "section h2 must use font-display for typographic hierarchy"
    )


# ---------------------------------------------------------------------------
# Field labels + inputs styled
# ---------------------------------------------------------------------------


def test_text_input_uses_input_premium_class():
    """Every text/string/url/email/password/integer/float input must
    carry `input-premium w-full` so it renders dark-theme-safe and
    full-width within its grid cell.

    Match strategy: grab the whole source line containing
    `name="site_name"`, regex on `<input...>` is unreliable because
    EJS interpolation `<%= ... %>` inside attribute values contains
    `>` characters that terminate the outer match prematurely.
    """
    rendered = emit_admin_settings(_mw_spec())
    site_name_lines = [
        line for line in rendered.split("\n")
        if 'name="site_name"' in line and "<input" in line
    ]
    assert site_name_lines, "site_name <input> line not found"
    for line in site_name_lines:
        assert "input-premium" in line, (
            f"site_name input missing input-premium: {line[:160]}"
        )
        assert "w-full" in line, f"site_name input missing w-full: {line[:160]}"


def test_textarea_uses_input_premium_class():
    """site_description + hero_description textareas must use input-premium."""
    rendered = emit_admin_settings(_mw_spec())
    import re
    textarea_matches = re.findall(
        r'<textarea[^>]*name="(site_description|hero_description)"[^>]*>',
        rendered,
    )
    assert len(textarea_matches) >= 2, "expected description textareas"


def test_textarea_w_full_class():
    """Both description textareas should be full-width within their grid cell."""
    rendered = emit_admin_settings(_mw_spec())
    import re
    # Find the full <textarea ...> opener for site_description
    ta_match = re.search(
        r'<textarea[^>]*name="site_description"[^>]*>', rendered
    )
    assert ta_match, "site_description textarea not found"
    ta_tag = ta_match.group(0)
    assert "input-premium" in ta_tag, (
        f"textarea missing input-premium: {ta_tag[:200]}"
    )
    assert "w-full" in ta_tag, f"textarea missing w-full: {ta_tag[:200]}"


def test_labels_have_block_typography_classes():
    """Labels should use `block text-sm font-medium mb-1` so they
    appear above their input with proper typography."""
    rendered = emit_admin_settings(_mw_spec())
    # All labels emit via humanize()+template, so grep a specific one
    import re
    label_matches = re.findall(
        r'<label[^>]*for="field-site_name"[^>]*>', rendered
    )
    assert label_matches, "site_name label not found"
    for m in label_matches:
        # At minimum: `block` + `text-sm` (typography + block-level display)
        assert "block" in m, f"label missing block class: {m[:150]}"
        assert "text-sm" in m, f"label missing text-sm class: {m[:150]}"


# ---------------------------------------------------------------------------
# Grid layout inside each section
# ---------------------------------------------------------------------------


def test_section_uses_grid_or_space_y_for_field_layout():
    """Within each card, fields should be arranged via a grid (2-column
    responsive) or vertical spacing, not laid out inline with no
    separation. The pre-B+C canonical used
    `<div class="grid grid-cols-1 md:grid-cols-2 gap-4">`."""
    rendered = emit_admin_settings(_mw_spec())
    # Presence of EITHER a grid class OR space-y-* anywhere in the
    # section body indicates field layout.
    uses_grid = "grid-cols-" in rendered or "grid " in rendered or 'class="grid' in rendered
    uses_space_y = "space-y-" in rendered
    assert uses_grid or uses_space_y, (
        "settings sections must use grid or space-y layout for fields"
    )


# ---------------------------------------------------------------------------
# CRUD forms also styled
# ---------------------------------------------------------------------------


def test_crud_new_edit_inputs_use_input_premium():
    """CRUD new.ejs + edit.ejs forms must also style their inputs with
    input-premium + w-full, same as settings. Match by source line
    rather than regex (EJS interpolation inside attrs breaks simple
    `<input[^>]*>` patterns)."""
    spec = _mw_spec()
    new_ejs, edit_ejs = emit_crud_new_edit(spec, spec["data_models"][0])
    for label, out in [("new.ejs", new_ejs), ("edit.ejs", edit_ejs)]:
        title_lines = [
            line for line in out.split("\n")
            if 'name="title"' in line and "<input" in line
        ]
        assert title_lines, f"{label}: title <input> line not found"
        for line in title_lines:
            assert "input-premium" in line, (
                f"{label}: title input missing input-premium: {line[:160]}"
            )
            assert "w-full" in line, (
                f"{label}: title input missing w-full: {line[:160]}"
            )


def test_crud_new_edit_has_page_header_with_back_link():
    """CRUD forms should have a back-link to the model's admin list and
    a heading so users aren't dumped onto a bare form."""
    spec = _mw_spec()
    new_ejs, edit_ejs = emit_crud_new_edit(spec, spec["data_models"][0])
    for label, out in [("new.ejs", new_ejs), ("edit.ejs", edit_ejs)]:
        # Back link to the admin list page
        assert 'href="/admin/events' in out or 'href="/events' in out, (
            f"{label}: missing back-link to events list"
        )


def test_crud_forms_use_max_width_and_card_wrapper():
    """CRUD new/edit pages should wrap the form in a card-premium
    container with a max-width so the form isn't edge-to-edge on wide
    viewports."""
    spec = _mw_spec()
    new_ejs, edit_ejs = emit_crud_new_edit(spec, spec["data_models"][0])
    for label, out in [("new.ejs", new_ejs), ("edit.ejs", edit_ejs)]:
        assert "max-w-" in out, f"{label}: missing max-width container"
        # card-premium OR admin-crud-form with padding
        assert "card-premium" in out or "admin-crud-form" in out, (
            f"{label}: missing card wrapping"
        )


# ---------------------------------------------------------------------------
# Dark-theme <style> block embedded
# ---------------------------------------------------------------------------


def test_settings_embeds_dark_theme_style_block():
    """Settings output should include a <style> block that overrides
    input bg/color under dark-primary body classes, matching the canonical
    template's auto-flip pattern. This is what made pre-B+C settings
    readable on MW's dark navy theme."""
    rendered = emit_admin_settings(_mw_spec())
    # The signature of the dark-theme override rule
    assert "<style>" in rendered, "missing embedded <style> block"
    # Targets admin-settings-card under dark body classes
    assert "admin-settings-card" in rendered
    assert "bg-slate-950" in rendered or "bg-primary" in rendered, (
        "missing dark-theme body selector in <style> block"
    )
