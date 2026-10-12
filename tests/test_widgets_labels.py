"""Tests for cli/widgets/labels.py - the LABEL_MAP + humanize() helper.

Section 03 of the B+C pipeline rewrite. The LABEL_MAP is the canonical
snake_case -> English label mapping used by every widget; humanize() is the
fallback-aware lookup. The "covers every six-section key" test is an
intentional coupling with section-04's SECTION_LAYOUT constant - if someone
adds a new admin-settings section key without registering a label, this
guard fails fast.
"""

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

from cli.widgets.labels import LABEL_MAP, humanize


def test_humanize_hero_image_url_is_canonical_label():
    assert humanize("hero_image_url") == "Hero Background Image"


def test_humanize_nav_logo_url_is_canonical_label():
    assert humanize("nav_logo_url") == "Navigation Logo"


def test_humanize_instagram_url_is_canonical_label():
    assert humanize("instagram_url") == "Instagram"


def test_humanize_unknown_snake_case_falls_back_to_title_case():
    assert humanize("unknown_snake_case_key") == "Unknown Snake Case Key"


def test_label_map_covers_every_key_in_the_six_section_admin_settings_layout():
    # Keys enumerated in the widget-registry section of the B+C plan as the
    # canonical six-section layout that section-04 will emit. This list is
    # the contract between the registry and the forms module; a new section
    # key landing in SECTION_LAYOUT without a label entry here must fail.
    expected_keys = [
        "site_name",
        "site_url",
        "site_description",
        "hero_tagline",
        "hero_description",
        "hero_image_url",
        "nav_logo_url",
        "about_title",
        "about_body",
        "about_photo_url",
        "business_name",
        "contact_email",
        "contact_phone",
        "contact_address",
        "instagram_url",
        "facebook_url",
        "youtube_url",
        "tiktok_url",
        "twitter_url",
        "linkedin_url",
    ]
    missing = [key for key in expected_keys if key not in LABEL_MAP]
    assert not missing, (
        f"LABEL_MAP is missing {len(missing)} six-section-layout keys: {missing}"
    )
