"""Schema + shape validation for compose/data/ decision files.

These tests guarantee the JSON files are well-formed and that every
internal reference resolves (layout widget -> widget registry, palette
-> required CSS vars, etc).
"""

from __future__ import annotations

import pytest

from compose.build import (
    load_archetypes, load_palettes, load_font_pairs, load_layouts,
    load_widgets, get_archetype, get_palette, get_font_pair, get_layout,
    get_widget,
)


REQUIRED_PALETTE_VARS = {
    "--color-primary", "--color-on-primary", "--color-secondary",
    "--color-accent", "--color-foreground", "--color-foreground-muted",
    "--color-background", "--color-surface", "--color-border",
    "--color-error", "--color-success",
}


# ---- archetypes ----

def test_archetypes_load():
    data = load_archetypes()
    assert data["version"] >= 1
    assert len(data["archetypes"]) >= 3


def test_archetypes_have_required_fields():
    for a in load_archetypes()["archetypes"]:
        assert a["id"]
        assert a["name"]
        assert a["default_pages"]
        assert a["default_data_models"]
        assert a["default_palette_suggestions"]
        assert a["default_font_pair_suggestions"]
        assert a["copy_tone"]
        assert a.get("page_layouts")


def test_archetype_page_layouts_resolve():
    """Each archetype's referenced page layout must exist in layouts.json."""
    for a in load_archetypes()["archetypes"]:
        for page_id, layout_id in a["page_layouts"].items():
            get_layout(layout_id)  # raises if missing


def test_archetype_palette_suggestions_resolve():
    for a in load_archetypes()["archetypes"]:
        for pid in a["default_palette_suggestions"]:
            get_palette(pid)


def test_archetype_font_pair_suggestions_resolve():
    for a in load_archetypes()["archetypes"]:
        for pid in a["default_font_pair_suggestions"]:
            get_font_pair(pid)


def test_mvp_archetypes_present():
    """MVP covers Healer, Coach, Local Service per the plan."""
    ids = {a["id"] for a in load_archetypes()["archetypes"]}
    assert "healer-wellness" in ids
    assert "coach-consultant" in ids
    assert "local-service" in ids


# ---- palettes ----

def test_palettes_load():
    p = load_palettes()
    assert p["version"] >= 1
    assert len(p["palettes"]) >= 6


def test_each_palette_has_required_vars():
    for p in load_palettes()["palettes"]:
        missing = REQUIRED_PALETTE_VARS - set(p["vars"].keys())
        assert not missing, (
            f"palette {p['id']} missing required CSS vars: {missing}"
        )


def test_palette_color_values_look_like_hex():
    """--color-primary etc should be #RRGGBB hex."""
    import re
    hex_re = re.compile(r"^#[0-9a-fA-F]{6}$")
    for p in load_palettes()["palettes"]:
        for key in ("--color-primary", "--color-on-primary", "--color-background"):
            val = p["vars"][key]
            assert hex_re.match(val), f"{p['id']}.{key}={val!r} not hex"


# ---- font pairs ----

def test_font_pairs_load():
    fp = load_font_pairs()
    assert fp["version"] >= 1
    assert len(fp["font_pairs"]) >= 4


def test_each_font_pair_has_google_url():
    for fp in load_font_pairs()["font_pairs"]:
        assert fp["display"]["family"]
        assert fp["body"]["family"]
        assert fp["google_fonts_url"].startswith("https://fonts.googleapis.com/")


# ---- layouts ----

def test_layouts_load():
    la = load_layouts()
    assert la["version"] >= 1
    assert len(la["layouts"]) >= 6


def test_each_layout_widget_resolves():
    """Every widget id in every layout must exist in the registry."""
    for layout_id, layout in load_layouts()["layouts"].items():
        for w in layout["widgets"]:
            get_widget(w["id"])


# ---- widgets ----

def test_widgets_load():
    wr = load_widgets()
    assert wr["version"] >= 1
    assert len(wr["widgets"]) >= 15


def test_each_widget_file_exists():
    """Every widget registry entry must point at a real file on disk."""
    from compose.build import COMPOSE_DIR
    for wid, w in load_widgets()["widgets"].items():
        fp = COMPOSE_DIR / w["file"]
        assert fp.exists(), f"widget {wid} file missing: {fp}"


def test_each_widget_has_data_contract():
    for wid, w in load_widgets()["widgets"].items():
        assert "data_contract" in w, f"widget {wid} missing data_contract"


# ---- field types ----

def test_the_city_placeholder_reaches_the_admin_form_and_names_no_real_place():
    """A field type's placeholder is written into the admin create and edit
    forms of every model that has a field of that type, so it is sample
    text on a customer's site."""
    import json
    from compose.build import (
        COMPOSE_DIR, BuildSpec, compose_files, get_field_type, resolve_spec,
    )
    assert get_field_type("city")["placeholder"] == "Sample City"
    spec = json.loads((COMPOSE_DIR / "fixtures" / "sample-coach-spec.json").read_text(encoding="utf-8"))
    spec["data_models"] = [{"name": "Venue", "fields": [
        {"name": "title", "sql_type": "VARCHAR(120)", "required": True},
        {"name": "city", "field_type": "city"},
    ]}]
    files = dict(compose_files(resolve_spec(BuildSpec.from_dict(spec))))
    for view in ("views/venues/new.ejs", "views/venues/edit.ejs"):
        assert '"placeholder": "Sample City"' in files[view], view
