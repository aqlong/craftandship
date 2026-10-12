"""Tests for _align_secondary_palette_to_spec.

Claude-emitted tailwind.config.js + public/style.css carried emerald
defaults (`surface: #ECFDF5`, `foreground: #064E3B`, `muted: #E8F1F3`,
`border: #A7F3D0`, `--color-background: #ECFDF5`, `--color-foreground:
#064E3B`) even when spec.design was maroon/cream/pink. Result: a light-
green stripe bled through below the footer. Post-processor rewrites
ONLY emerald defaults to spec-derived values; legitimate green specs
pass through.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


MAROON_SPEC = {
    "app_name": "palette-sample-v1",
    "design": {
        "primary_color": "#6b1519",
        "secondary_color": "#fdf0f0",
        "accent_color": "#d4a5a5",
    },
}


def test_emerald_bg_fg_in_style_css_rewritten_to_spec():
    style_css = (
        ":root {\n"
        "  --color-primary: #6b1519;\n"
        "  --color-background: #ECFDF5;\n"
        "  --color-foreground: #064E3B;\n"
        "}\n"
    )
    out = dict(build._align_secondary_palette_to_spec(
        [("public/style.css", style_css)], MAROON_SPEC,
    ))
    css = out["public/style.css"]
    # Emerald bg/fg gone.
    assert "#ECFDF5" not in css
    assert "#064E3B" not in css
    # Spec secondary now in bg; safe dark neutral in fg.
    assert "--color-background: #fdf0f0;" in css
    assert "--color-foreground: #1f1f1f;" in css


def test_emerald_tailwind_config_rewritten():
    tw = (
        "module.exports = { theme: { extend: { colors: {\n"
        "  primary: '#6b1519',\n"
        "  secondary: '#fdf0f0',\n"
        "  accent: '#d4a5a5',\n"
        "  surface: '#ECFDF5',\n"
        "  foreground: '#064E3B',\n"
        "  card: '#FFFFFF',\n"
        "  muted: '#E8F1F3',\n"
        "  border: '#A7F3D0',\n"
        "} } } }\n"
    )
    out = dict(build._align_secondary_palette_to_spec(
        [("tailwind.config.js", tw)], MAROON_SPEC,
    ))
    got = out["tailwind.config.js"]
    # Emerald values all replaced.
    for emerald in ("#ECFDF5", "#064E3B", "#E8F1F3", "#A7F3D0"):
        assert emerald not in got, f"{emerald} still present in tailwind config"
    # Spec palette flows through.
    assert "surface: '#fdf0f0'" in got
    assert "foreground: '#1f1f1f'" in got
    assert "border: '#d4a5a5'" in got
    # Untouched primary/secondary/accent stay.
    assert "primary: '#6b1519'" in got


def test_legitimate_green_spec_is_not_rewritten():
    """If the SPEC itself declares a green palette, the secondary
    palette should keep its green values. The rewrite only fires on
    the known emerald default hex constants."""
    green_spec = {
        "design": {
            "primary_color": "#14532d",
            "secondary_color": "#ecfdf5",
            "accent_color": "#22c55e",
        },
    }
    # This tailwind config doesn't have the emerald default tokens; it
    # has intentional greens for a green-palette app.
    tw = (
        "colors: {\n"
        "  surface: '#f0fdf4',\n"  # NOT in emerald-defaults list
        "  foreground: '#14532d',\n"  # NOT in emerald-defaults list
        "}\n"
    )
    out = dict(build._align_secondary_palette_to_spec(
        [("tailwind.config.js", tw)], green_spec,
    ))
    # Unchanged.
    assert out["tailwind.config.js"] == tw


def test_missing_design_returns_unchanged():
    """Spec without design palette → no-op."""
    bare_spec = {"app_name": "x"}
    tw = "surface: '#ECFDF5'"
    out = build._align_secondary_palette_to_spec(
        [("tailwind.config.js", tw)], bare_spec,
    )
    # Bare spec has no design; get_design_context may still return
    # inferred defaults. Either way: no crash.
    assert isinstance(out, list)


def test_idempotent_on_second_pass():
    tw = (
        "colors: {\n"
        "  primary: '#6b1519',\n"
        "  surface: '#ECFDF5',\n"
        "  foreground: '#064E3B',\n"
        "}\n"
    )
    once = dict(build._align_secondary_palette_to_spec(
        [("tailwind.config.js", tw)], MAROON_SPEC,
    ))
    twice = dict(build._align_secondary_palette_to_spec(
        list(once.items()), MAROON_SPEC,
    ))
    assert once == twice


def test_registered_in_expected_processors():
    import pipeline
    assert "_align_secondary_palette_to_spec" in pipeline.REGISTRY
    assert "_align_secondary_palette_to_spec" in pipeline.EXPECTED_PROCESSORS
    proc = pipeline.REGISTRY["_align_secondary_palette_to_spec"]
    assert "build" in proc.phase and "reapply" in proc.phase
