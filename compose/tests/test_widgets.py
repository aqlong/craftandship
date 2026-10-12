"""Unit tests for individual widget EJS files.

Each widget is verified for:
- File presence.
- HTML output when rendered with its data contract (min-valid + max-valid).
- Accessibility attrs where applicable.
- No unresolved EJS tokens in output.

Rendered via a Node subprocess that invokes a tiny runner. If Node or the
`ejs` package (installed by `cd web && npm ci`) isn't available, these tests
skip. The widget-unit-test discipline still lives here for the
pipeline-integration tests that compose full pages; if a widget's data
contract is wrong, compose will fail with a clear error.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from compose.build import COMPOSE_DIR, WIDGET_DIR, load_widgets, get_widget


# The renderer needs the `ejs` package, which web/ installs (`cd web && npm ci`).
EJS_PARENT = COMPOSE_DIR.parent / "web" / "node_modules"


def _node_available() -> bool:
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def _render_widget(widget_id: str, data: dict) -> str:
    """Spawn Node, render the widget EJS with given data, return HTML."""
    widget = get_widget(widget_id)
    widget_path = COMPOSE_DIR / widget["file"]
    ejs_cli = COMPOSE_DIR / "tests" / "_ejs_render.js"

    env = os.environ.copy()
    env["NODE_PATH"] = str(EJS_PARENT)

    r = subprocess.run(
        ["node", str(ejs_cli), str(widget_path), json.dumps(data)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"widget render failed: {r.stderr}")
    return r.stdout


# ---- Static checks (no Node needed) ----

def test_every_widget_file_exists():
    for wid, w in load_widgets()["widgets"].items():
        fp = COMPOSE_DIR / w["file"]
        assert fp.exists(), f"{wid}: file {w['file']} missing"


def test_every_widget_has_header_comment():
    """Each widget EJS starts with a compose:widget marker comment."""
    for wid, w in load_widgets()["widgets"].items():
        if not w["file"].endswith(".ejs"):
            continue
        content = (COMPOSE_DIR / w["file"]).read_text()
        assert "compose:widget" in content[:200], f"{wid} missing compose:widget marker"


def test_widgets_have_no_unresolved_template_vars_in_source():
    """Scan widget sources for literal `${}` or unmatched EJS tokens
    that would leak through to HTML output."""
    pattern_bad = re.compile(r"\$\{[^}]*\}")
    for wid, w in load_widgets()["widgets"].items():
        if not w["file"].endswith(".ejs"):
            continue
        content = (COMPOSE_DIR / w["file"]).read_text()
        assert not pattern_bad.search(content), (
            f"{wid}: has JS template-literal ${{}} which would NOT be "
            "interpolated; EJS uses <%= %> syntax"
        )


# ---- Rendered-output checks (Node subprocess) ----

@pytest.fixture(scope="module")
def node_env():
    if not _node_available():
        pytest.skip("node is not on PATH")
    if not (EJS_PARENT / "ejs").exists():
        pytest.skip("web/node_modules/ejs is not installed (run `cd web && npm ci`)")


def test_hero_centered_renders_heading(node_env):
    out = _render_widget("hero-centered", {
        "heading": "My Heading",
        "description": "My description",
        "cta_primary": {"text": "Go", "href": "/go"},
    })
    assert "My Heading" in out
    assert "<h1" in out
    assert "My description" in out


def test_hero_centered_renders_without_ctas(node_env):
    out = _render_widget("hero-centered", {
        "heading": "H", "description": "D"
    })
    assert "My description" not in out
    assert 'href="/go"' not in out


def test_offerings_grid_renders_items(node_env):
    out = _render_widget("offerings-grid-3col", {
        "heading": "Services",
        "items": [
            {"title": "Service A", "description": "Desc A"},
            {"title": "Service B", "description": "Desc B"},
        ]
    })
    assert "Services" in out
    assert "Service A" in out
    assert "Service B" in out


def test_testimonials_stack_renders_quotes(node_env):
    out = _render_widget("testimonials-stack", {
        "heading": "Voices",
        "items": [
            {"quote": "Great!", "author_name": "Alice", "service": "1:1"},
        ]
    })
    assert "Great!" in out
    assert "Alice" in out
    assert "1:1" in out


def test_testimonials_stack_hides_sample_placeholders(node_env):
    """Content-integrity: the archetype default '(sample)' / draft-slot
    testimonials must NEVER render on the public site (a defect found on
    2026-06-07 by the automated build review, a loop that ran outside this
    repository and is not included in it). All-placeholder -> the whole
    section renders nothing."""
    out = _render_widget("testimonials-stack", {
        "heading": "Voices",
        "items": [
            {"quote": "A draft client reflection slot. Replace with a real, consented quote when you collect one.",
             "author_name": "Client (sample)", "service": "Session"},
            {"quote": "Another draft slot. A quote naming the modality usually reads stronger.",
             "author_name": "Client (sample)", "service": "Energy work"},
        ]
    })
    assert "Client (sample)" not in out
    assert "draft" not in out.lower()
    assert "Replace with a real" not in out
    # all rows are placeholders -> no section + no heading rendered at all
    assert "Voices" not in out


def test_testimonials_stack_shows_real_hides_sample_in_mix(node_env):
    """A mix of real + placeholder shows only the real ones."""
    out = _render_widget("testimonials-stack", {
        "heading": "Voices",
        "items": [
            {"quote": "The breathwork sessions changed how I sleep.", "author_name": "Maya R.", "service": "1:1"},
            {"quote": "A draft slot. Replace with a real quote.", "author_name": "Client (sample)", "service": "Session"},
        ]
    })
    assert "changed how I sleep" in out
    assert "Maya R." in out
    assert "Client (sample)" not in out
    assert "Voices" in out  # header shows because a real testimonial exists


def test_contact_form_has_csrf_and_honeypot_safe(node_env):
    out = _render_widget("form-contact", {
        "heading": "Contact",
        "service_options": ["A", "B"],
    })
    assert 'name="_csrf"' in out
    assert 'value="test-csrf"' in out
    assert 'action="/contact"' in out
    assert '<option value="A">A</option>' in out


def test_contact_form_renders_six_social_glyphs_when_settings_populated(node_env):
    """Healer/SMB-relevant socials: Instagram + TikTok + YouTube + Email
    + Phone + Venmo all render as conditional <a> glyphs in the right
    sidebar's social row when their corresponding settings are present.
    """
    out = _render_widget("form-contact", {
        "heading": "Contact",
        "service_options": [],
        "settings": {
            "instagram_url": "https://instagram.com/example",
            "tiktok_url": "https://tiktok.com/@example",
            "youtube_url": "https://youtube.com/@example",
            "contact_email": "hi@example.com",
            "contact_phone": "(555) 555-0123",
            "venmo_handle": "@example",
        },
    })
    assert 'aria-label="Instagram"' in out
    assert 'aria-label="TikTok"' in out
    assert 'aria-label="YouTube"' in out
    assert 'aria-label="Email"' in out
    assert 'aria-label="Phone"' in out
    assert 'aria-label="Venmo"' in out
    # Venmo handle "@example" must strip the leading @ in the URL but
    # preserve the username, and use venmo.com/u/<handle> profile form.
    assert 'href="https://venmo.com/u/example"' in out
    # Email + phone glyphs must use the right URL scheme.
    assert 'href="mailto:hi@example.com"' in out
    assert 'href="tel:5555550123"' in out


def test_contact_form_omits_glyphs_when_settings_missing(node_env):
    """The new social-row glyphs are STRICTLY conditional: when none of
    the social/contact settings are populated, no aria-label glyphs render.
    """
    out = _render_widget("form-contact", {
        "heading": "Contact",
        "service_options": [],
        "settings": {},
    })
    assert 'aria-label="TikTok"' not in out
    assert 'aria-label="YouTube"' not in out
    assert 'aria-label="Venmo"' not in out


def test_login_form_renders_email_and_password_fields(node_env):
    out = _render_widget("form-login", {})
    assert 'type="email"' in out
    assert 'type="password"' in out
    assert 'action="/login"' in out


def test_admin_dashboard_renders_stats(node_env):
    """MW v25 pattern: dashboard renders stat-card grid with /admin/<slug>
    hrefs. Labels are title-cased for display (POSTS → Posts). Count is
    rendered as a large number; cards have a colored top stripe."""
    out = _render_widget("admin-dashboard", {
        "stats": [
            {"label": "POSTS", "count": 3, "manage_href": "/admin/blog-posts"},
            {"label": "EVENTS", "count": 5, "manage_href": "/admin/events"},
        ],
        "recent_inquiries": [],
        "quick_links": [],
        "upcoming_events": [],
    })
    assert "Posts" in out
    assert "Events" in out
    # Numeric count + Manage link
    assert ">3<" in out
    assert ">5<" in out
    assert 'href="/admin/blog-posts"' in out
    assert 'href="/admin/events"' in out
    assert "Manage" in out


def test_admin_crud_list_renders_empty_state(node_env):
    out = _render_widget("admin-crud-list", {
        "resource_name_plural": "Events",
        "resource_slug": "events",
        "columns": [{"key": "title", "label": "Title"}],
        "rows": []
    })
    assert "No events yet" in out.lower() or "events yet" in out.lower()


def test_admin_crud_list_renders_rows(node_env):
    """MW v25 pattern: crud-list (rendered only on /admin/<slug>) uses
    /admin/<slug>/<id>/... URLs for view/edit/delete."""
    out = _render_widget("admin-crud-list", {
        "resource_name_plural": "Blog Posts",
        "resource_slug": "blog-posts",
        "columns": [{"key": "title", "label": "Title"}],
        "rows": [{"id": 1, "title": "Hello"}]
    })
    assert "Hello" in out
    assert 'href="/admin/blog-posts/1"' in out
    assert 'href="/admin/blog-posts/1/edit"' in out
    assert 'action="/admin/blog-posts/1/delete"' in out


def test_error_404_renders(node_env):
    out = _render_widget("error-404", {})
    assert "404" in out
    assert 'href="/"' in out


def test_footer_renders_nav_items(node_env):
    out = _render_widget("shell-footer", {
        "nav_items": [{"label": "Home", "href": "/"}, {"label": "About", "href": "/about"}],
    })
    assert ">Home<" in out
    assert ">About<" in out


def test_nav_public_logged_out_shows_login(node_env):
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
    })
    assert "Login" in out
    assert "Logout" not in out


def test_nav_public_logged_in_shows_admin_link_and_logout(node_env):
    """MW v25 pattern: logged-in users see an 'Admin' link (single entry
    to admin dashboard) plus Logout. No 'Login' link visible."""
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
        "user": {"email": "admin@x.example", "role": "admin"},
    })
    assert ">Admin<" in out
    assert "Logout" in out
    assert 'href="/admin"' in out
    assert 'href="/login"' not in out


def test_nav_public_logged_in_non_admin_hides_admin_link(node_env):
    """Logged-in non-admin user should NOT see the Admin link; only admins get it."""
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
        "user": {"email": "user@x.example", "role": "editor"},
    })
    assert 'href="/admin"' not in out
    assert "Logout" in out  # still logged in, still gets logout


def _nav_public_settings(extra=None):
    """Build a settings dict that mirrors what the seed emits + lets a
    test override individual keys (e.g. nav_logo_url, show_nav_logo)."""
    base = {
        "site_name": "Sample Irrigation Co",
        "site_description": "desc",
        "contact_email": "a@b.c",
        "_palette_vars": {"--color-primary": "#6b1519", "--color-on-primary": "#fff"},
        "_fonts": {"display": {"family": "Rubik"}, "body": {"family": "Nunito Sans"}},
        "_fonts_url": "https://fonts.googleapis.com/css2?...",
    }
    if extra:
        base.update(extra)
    return base


def test_nav_public_default_no_nav_logo_url_renders_wordmark_only(node_env):
    """Default seed: when nav_logo_url is empty, nav renders the wordmark
    only; the monogram circle is opt-in, not default."""
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
        "settings": _nav_public_settings({"nav_logo_url": ""}),
    })
    assert 'class="nav-logo"' not in out, (
        "monogram circle must not render when nav_logo_url is empty"
    )
    # Wordmark still renders
    assert "Sample Irrigation Co" in out


def test_nav_public_default_logo_svg_renders_wordmark_only(node_env):
    """Default seed value for nav_logo_url is `/logo.svg?v=<hash>` (the
    auto-generated initials monogram). That's the default, not a real
    customer brand mark, so wordmark-only is the default render."""
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
        "settings": _nav_public_settings({
            "nav_logo_url": "/logo.svg?v=abc12345",
        }),
    })
    assert 'class="nav-logo"' not in out, (
        "monogram circle must not render for the default /logo.svg URL "
        "(it's the auto-generated initials monogram, not a customer brand mark)"
    )
    # wordmark still renders
    assert "Sample Irrigation Co" in out
    # no <img> tag pointing at the default monogram
    assert 'src="/logo.svg' not in out


def test_nav_public_show_nav_logo_true_renders_monogram(node_env):
    """When settings.show_nav_logo='true' (admin-toggled opt-in), the
    monogram circle DOES render; with initials over the default
    /logo.svg, or an <img> if the URL is real."""
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
        "settings": _nav_public_settings({
            "nav_logo_url": "/logo.svg?v=abc12345",
            "show_nav_logo": "true",
        }),
    })
    assert 'class="nav-logo"' in out, (
        "monogram circle must render when show_nav_logo='true'"
    )
    # /logo.svg is the default monogram; renders <img>
    assert 'src="/logo.svg?v=abc12345"' in out


def test_nav_public_real_uploaded_logo_renders_monogram_image(node_env):
    """A customer-uploaded nav_logo_url (anything not /logo.svg and not
    a placeholder) renders the monogram circle with their logo as an
    <img>; no opt-in flag needed."""
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
        "settings": _nav_public_settings({
            "nav_logo_url": "/uploads/brand-mark.png",
        }),
    })
    assert 'class="nav-logo"' in out
    assert 'src="/uploads/brand-mark.png"' in out


def test_nav_public_placeholder_url_renders_wordmark_only(node_env):
    """Placeholder URLs (picsum / lorem / placeholder / etc.) are not
    real customer logos. Without show_nav_logo='true', wordmark only."""
    out = _render_widget("shell-nav-public", {
        "nav_items": [{"label": "Home", "href": "/"}],
        "settings": _nav_public_settings({
            "nav_logo_url": "https://picsum.photos/seed/x/120/120",
        }),
    })
    assert 'class="nav-logo"' not in out


def test_admin_subnav_renders_nav_items(node_env):
    out = _render_widget("shell-admin-subnav", {
        "admin_nav_items": [
            {"label": "Dashboard", "href": "/admin"},
            {"label": "Blog Posts", "href": "/admin/blog-posts"},
        ],
        "currentPath": "/admin/blog-posts",
    })
    assert "Dashboard" in out
    assert "Blog Posts" in out
    assert 'href="/admin/blog-posts"' in out


def test_photo_gallery_grid_renders_items(node_env):
    out = _render_widget("photo-gallery-grid", {
        "heading": "Gallery",
        "items": [
            {"image_url": "https://example.com/p1.jpg", "title": "Photo 1", "caption": "One"},
            {"image_url": "https://example.com/p2.jpg", "title": "Photo 2", "caption": "Two"},
        ],
    })
    assert "Photo 1" in out
    assert "Photo 2" in out
    assert "https://example.com/p1.jpg" in out
    assert "https://example.com/p2.jpg" in out


def test_photo_gallery_grid_renders_empty_state(node_env):
    out = _render_widget("photo-gallery-grid", {"items": []})
    assert "No photos yet" in out


def test_admin_quick_links_renders_items(node_env):
    out = _render_widget("admin-quick-links", {
        "items": [
            {"label": "Manage Posts", "href": "/admin/posts", "description": "Edit posts"},
            {"label": "Site Settings", "href": "/admin/settings", "description": "Tweak brand"},
        ],
    })
    assert "Manage Posts" in out
    assert "Site Settings" in out
    assert 'href="/admin/posts"' in out


def test_admin_tools_row_has_change_password_and_danger_zone(node_env):
    out = _render_widget("admin-tools-row", {"csrfToken": "abc"})
    assert "Change Password" in out
    assert 'action="/admin/change-password"' in out
    assert 'action="/admin/clear-all"' in out
    assert "Danger Zone" in out
    # The destructive confirm is a data-confirm attribute (handled by the
    # delegated guard in cas-form-validate.js), NOT an inline onsubmit that
    # the app CSP would block.
    assert "data-confirm=" in out
    assert "onsubmit=" not in out


def test_admin_upload_zone_renders_preview_when_current_url(node_env):
    out = _render_widget("admin-upload-zone", {
        "field_name": "hero_image_url",
        "label": "Hero Image",
        "current_url": "/uploads/2026/04/test.jpg",
    })
    assert '/uploads/2026/04/test.jpg' in out
    # Hidden input for form submission
    assert 'name="hero_image_url"' in out
    # Preview visible (block), prompt hidden (none)
    assert "display: block" in out
    # Interactions are wired by /js/upload-zone.js via the wrapper's
    # data-field + element ids; the widget carries NO inline on*= handlers
    # (those are blocked by the generated app's script-src nonce CSP).
    assert 'data-field="hero_image_url"' in out
    assert "onclick=" not in out
    assert "onchange=" not in out
    assert "ondrop=" not in out


def test_admin_upload_zone_renders_prompt_when_empty(node_env):
    out = _render_widget("admin-upload-zone", {
        "field_name": "logo",
        "label": "Logo",
        "current_url": "",
    })
    # Prompt visible, preview hidden
    assert "Drag and drop" in out
    assert 'name="logo"' in out


def test_admin_upcoming_events_renders_events(node_env):
    out = _render_widget("admin-upcoming-events", {
        "events": [
            {"id": 1, "title": "Moon Circle", "event_date": "2026-07-12", "location": "Town Hall"},
        ],
    })
    assert "Moon Circle" in out
    assert "Town Hall" in out
    assert 'href="/admin/events/1/edit"' in out
