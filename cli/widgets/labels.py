"""Canonical snake_case -> English label mapping for widget-rendered forms.

This module is the single source of truth for admin-settings field labels.
The mapping is copied from cli/build.py::_SETTINGS_LABEL_MAP so widget
output is byte-identical to what the FIX M post-processor
(_humanize_settings_labels) emits today. Keeping both definitions in sync
is covered by a byte-identical-output test in section-04; for now this
module's tests assert the canonical entries and full six-section-layout
coverage.

humanize(name) is the universal label resolver every widget calls. Unknown
keys fall back to Title Case of the snake_case name so widgets never emit
raw snake_case to customers.
"""

from __future__ import annotations


# Keep this dict sorted by the six-section layout order defined in
# cli/widgets/forms.py::SECTION_LAYOUT so future edits stay legible.
# Every key used in that layout must appear here; the
# test_label_map_covers_every_key_in_the_six_section_admin_settings_layout
# test enforces it.
LABEL_MAP: dict[str, str] = {
    # Site Identity
    "site_name": "Site Name",
    "site_url": "Site URL",
    "site_description": "Site Description",
    # Hero section
    "hero_tagline": "Hero Tagline",
    "hero_description": "Hero Description",
    "hero_image_url": "Hero Background Image",
    "hero_image": "Hero Background Image",
    # Logo and About
    "nav_logo_url": "Navigation Logo",
    "nav_logo": "Navigation Logo",
    "about_title": "About Page Title",
    "about_body": "About Page Body",
    "about_photo_url": "About Photo",
    "about_photo": "About Photo",
    # Business / Contact
    "business_name": "Business Name",
    "business_email": "Business Email",
    "business_phone": "Business Phone",
    "business_address": "Business Address",
    "contact_email": "Contact Email",
    "contact_phone": "Contact Phone",
    "contact_address": "Contact Address",
    # Social Media
    "instagram_url": "Instagram",
    "facebook_url": "Facebook",
    "youtube_url": "YouTube",
    "tiktok_url": "TikTok",
    "twitter_url": "Twitter / X",
    "linkedin_url": "LinkedIn",
    "pinterest_url": "Pinterest",
    # Miscellaneous
    "booking_url": "Booking URL",
    "newsletter_url": "Newsletter Signup URL",
}


def humanize(name: str) -> str:
    """Return the canonical English label for `name`, or Title Case fallback.

    >>> humanize("hero_image_url")
    'Hero Background Image'
    >>> humanize("some_unknown_field")
    'Some Unknown Field'
    """
    if name in LABEL_MAP:
        return LABEL_MAP[name]
    # Fallback: split snake_case, title-case each token, rejoin with spaces.
    return " ".join(token.capitalize() for token in name.split("_") if token)
