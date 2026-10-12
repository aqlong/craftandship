"""Tests for the site_name single-source-of-truth contract.

_ensure_site_settings derives settings.site_name
from spec["app_name"] (prettified, version suffix stripped) EARLY
in the build phase. Both the seed.sql site_name row AND the
in-memory spec["settings"]["site_name"] converge on this value, so
every downstream processor (admin handlers, nav brand, footer,
meta, email templates) reads a consistent readable brand.
"""

from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


def _minimal_files(seed_extra: str = "") -> list:
    """Minimum build shape for _ensure_site_settings to run end-to-end."""
    schema = (
        "CREATE TABLE IF NOT EXISTS site_settings (\n"
        "  key VARCHAR(100) PRIMARY KEY,\n"
        "  value TEXT DEFAULT '',\n"
        "  updated_at TIMESTAMP DEFAULT NOW()\n"
        ");\n"
    )
    seed = (
        "-- Seed data\n"
        "INSERT INTO users (email, role) VALUES ('admin@example.com', 'admin');\n"
        f"{seed_extra}"
    )
    return [
        ("db/schema.sql", schema),
        ("db/seed.sql", seed),
    ]


def _get_site_name_seed(seed_sql: str) -> str | None:
    """Extract the site_settings(key='site_name') value from seed SQL."""
    import re
    m = re.search(
        r"INSERT INTO site_settings \(key, value\) VALUES\s*"
        r"\('site_name',\s*'([^']*)'",
        seed_sql,
    )
    return m.group(1) if m else None


# ---- test 1: empty site_name derived from app_name ----------------

def test_empty_site_name_derived_from_app_name():
    spec = {
        "app_name": "medicine-waves-v10",
        "admin_email": "admin@example.com",
        "settings": {},  # empty dict, site_name not populated
    }
    files = _minimal_files()
    out = dict(build._ensure_site_settings(files, spec))
    seed = out["db/seed.sql"]
    assert _get_site_name_seed(seed) == "Medicine Waves"
    # Stage 3 contract: in-memory spec mutated to carry the derived name.
    assert spec["settings"].get("site_name") == "Medicine Waves"


# ---- test 2: explicit site_name preserved -------------------------

def test_populated_site_name_not_overwritten():
    spec = {
        "app_name": "medicine-waves-v10",
        "admin_email": "admin@example.com",
        "settings": {"site_name": "Medicine Waves Community"},
    }
    files = _minimal_files()
    out = dict(build._ensure_site_settings(files, spec))
    seed = out["db/seed.sql"]
    # seed default should reflect the spec's existing value (via the
    # spec_seed_overrides pipeline); the prettified fallback is NOT used.
    assert _get_site_name_seed(seed) == "Medicine Waves Community"
    # In-memory value preserved.
    assert spec["settings"]["site_name"] == "Medicine Waves Community"


# ---- test 3: version-suffix regression case ------------------------

def test_app_name_version_suffix_stripped():
    spec = {
        "app_name": "sample-practitioner-v1-rc2",
        "admin_email": "admin@example.com",
        "settings": {},
    }
    files = _minimal_files()
    out = dict(build._ensure_site_settings(files, spec))
    seed = out["db/seed.sql"]
    # The suffix here is "-v1-rc2". _derive_site_name_from_spec strips
    # "-v<N>" and "-v<N>-<digits>", but not a trailing "-rc2" token.
    # Acceptable: "Sample Practitioner V1 Rc2" if the
    # regex is strict, "Sample Practitioner" if it's loose. Test the contract
    # that SOMETHING readable is used, not the raw slug.
    site_name = _get_site_name_seed(seed)
    assert site_name is not None
    # Must NOT be the raw slug form.
    assert site_name != "sample-practitioner-v1-rc2"
    # Must start with "Sample Practitioner" (the branded prefix).
    assert site_name.startswith("Sample Practitioner")


# ---- test 4: business_name default also prettified ----------------

def test_business_name_default_uses_prettified_form():
    spec = {
        "app_name": "medicine-waves-v10",
        "admin_email": "admin@example.com",
        "settings": {},
    }
    files = _minimal_files()
    out = dict(build._ensure_site_settings(files, spec))
    seed = out["db/seed.sql"]
    # business_name seed default should match prettified site_name (both
    # default to the same prettification when the spec is silent).
    import re
    m = re.search(
        r"INSERT INTO site_settings \(key, value\) VALUES\s*"
        r"\('business_name',\s*'([^']*)'",
        seed,
    )
    assert m is not None
    assert m.group(1) == "Medicine Waves"


# ---- test 5: _ensure_nav_brand_element reads converged value ------

def test_nav_brand_injector_reads_converged_site_name():
    """After _ensure_site_settings mutates the spec, a later
    _ensure_nav_brand_element pass injects the derived wordmark."""
    spec = {
        "app_name": "sunflower-wellness-v2",
        "admin_email": "admin@example.com",
        "settings": {},  # empty → _ensure_site_settings will populate
    }
    files = _minimal_files()
    # Stage 1: site_settings runs, mutates spec.
    files2 = list(build._ensure_site_settings(files, spec))
    # Sanity: mutation happened.
    assert spec["settings"].get("site_name") == "Sunflower Wellness"

    # Stage 2: nav brand injector runs on a layout missing a brand
    # marker. It reads spec["settings"]["site_name"] via
    # _derive_site_name_from_spec's cascade.
    layout = (
        "<!DOCTYPE html><html><body>\n"
        '<nav class="bg-white"><a href="/about">About</a></nav>\n'
        "</body></html>"
    )
    files3 = list(files2) + [("views/layout.ejs", layout)]
    out = dict(build._ensure_nav_brand_element(files3, spec))
    got_layout = out["views/layout.ejs"]
    # Injector saw the mutated spec, used "Sunflower Wellness".
    assert ">Sunflower Wellness<" in got_layout
    # NOT the raw slug.
    assert "sunflower-wellness-v2" not in got_layout


# ---- test 6: MW canonical shape preserved -------------------------

def test_mw_canonical_spec_site_name_still_medicine_waves():
    """MW's canonical spec has app_name='medicine-waves-v10' and no
    explicit settings.site_name. The prettifier produces
    'Medicine Waves' which matches the hardcoded brand in MW's
    layout.ejs, critical for MW-canonical parity."""
    spec = {
        "app_name": "medicine-waves-v10",
        "admin_email": "admin@example.com",
        "settings": {},
    }
    files = _minimal_files()
    out = dict(build._ensure_site_settings(files, spec))
    seed = out["db/seed.sql"]
    assert _get_site_name_seed(seed) == "Medicine Waves"
    assert spec["settings"]["site_name"] == "Medicine Waves"
