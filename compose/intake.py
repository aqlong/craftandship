"""compose/intake.py: v2 intake + v1→v2 translator.

Two entry points, both producing a v2 spec ready for
`compose.build.build_app`:

1. `scaffold_spec(answers)`: direct CLI: answers.json → v2 spec.
2. `translate_v1_spec(v1_spec)`: reuse the existing web/ intake
   flow (Claude conversational intake produces a v1 AppSpec) and
   translate into a v2 spec. Same intake UX, v2-compatible output.

No Claude calls in this module. Copy drafting stays in web/ via
the existing intake conversation; this module only does shape
translation + defaults.

Inputs (answers.json or stdin):
  {
    "app_name":     "sample-coach",            # required, kebab-case
    "archetype":    "coach-consultant",        # required, must exist
    "site_name":    "Mira Lenox Coaching",     # required
    "site_description": "Executive coaching for...",
    "hero_tagline": "Clarity under pressure",  # short
    "hero_description": "I help C-suite...",   # 1-2 sentences
    "about_title":  "About Mira",              # defaults reasonable
    "about_body":   "Twenty years ...",
    "admin_email":  "admin@miralenoxcoaching.example",
    "contact_email":"hello@miralenoxcoaching.example",
    "contact_phone":"(555) 555-0120",
    "city_state":   "Sample City, ST",
    "palette":      "slate-gold",              # optional, picks archetype default
    "font_pair":    "playfair-source-sans",    # optional, picks archetype default
    "services":     [ {...}, ... ],            # optional, archetype seeds some
    "testimonials": [ {...}, ... ],            # optional
    "social": { "linkedin_url": "...", ... }   # optional
  }

Output:
  Writes `compose/fixtures/sample-<app_name>-spec.json` and returns the
  dict. Spec validates clean via `validate_spec(spec)`.

Usage (CLI):
  python -m compose.intake answers.json
  python -m compose.intake --stdin < answers.json
  python -m compose.intake --build answers.json   # also runs compose.build.build_app

The goal is: one small JSON of brand basics → a full pipeline-ready spec
with every page, every section, every data model seeded with sensible
defaults pulled from the archetype + copy-tone + palette. The founder
can iterate on answers.json, re-run intake, and rebuild in seconds.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from compose.build import (
    load_archetypes, load_palettes, load_font_pairs,
    get_archetype, build_app, validate_spec, BuildSpec,
    _is_parametric_path, _is_safe_route_path, _sample_date,
    _page_id_reservation, _path_key, _roles_imply_login,
    _json_size_errors,
)
from compose.markdown_safe import sanitize_html_fragment


COMPOSE_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = COMPOSE_DIR / "fixtures"


def _default_services(archetype_id: str) -> list:
    """Seed 3 plausible services per archetype so the offerings-grid-3col
    widget has real content out of the box. The founder edits after."""
    if archetype_id == "coach-consultant":
        return [
            {"label": "INDIVIDUAL", "title": "1:1 Executive Coaching", "description": "12-week coaching container focused on leadership identity, decision-making, and presence.", "price_display": "$4,800", "cta_text": "Apply", "cta_href": "/contact?service=1%3A1+Coaching"},
            {"label": "TEAM", "title": "Leadership Intensives", "description": "Half-day + full-day offsites for leadership teams. Alignment, strategy, hard conversations.", "price_display": "From $8,500", "cta_text": "Inquire", "cta_href": "/contact?service=Team+Intensive"},
            {"label": "KEYNOTE", "title": "Speaking & Workshops", "description": "Keynote talks and workshops on high-stakes leadership.", "price_display": "Inquire", "cta_text": "Book", "cta_href": "/contact?service=Keynote"},
        ]
    if archetype_id == "healer-wellness":
        return [
            {"label": "INDIVIDUAL", "title": "1:1 Sessions", "description": "60-minute one-to-one sessions shaped around what you need that day.", "price_display": "$140", "cta_text": "Book", "cta_href": "/contact?service=1%3A1+Sessions"},
            {"label": "CONTAINER", "title": "Mentorship", "description": "A three-month program with regular check-ins for steady, longer-term support.", "price_display": "Apply", "cta_text": "Inquire", "cta_href": "/contact?service=Mentorship"},
            {"label": "EVENTS", "title": "Circles & Festivals", "description": "Group circles and collaborative workshops.", "price_display": "Collaborate", "cta_text": "Get in touch", "cta_href": "/contact?service=Events"},
        ]
    if archetype_id in ("local-service", "tradesperson-contractor"):
        return [
            {"label": "REPAIRS", "title": "Repairs & Diagnostics", "description": "Come out, diagnose, quote, fix.", "price_display": "from $149", "cta_text": "Book", "cta_href": "/contact"},
            {"label": "INSTALL", "title": "Installation & Replacement", "description": "New installs + full replacements.", "price_display": "Inquire", "cta_text": "Quote", "cta_href": "/contact"},
            {"label": "SERVICE", "title": "Service Calls", "description": "Standard service calls within the service area.", "price_display": "Inquire", "cta_text": "Call", "cta_href": "/contact"},
        ]
    if archetype_id == "therapist-counselor":
        return [
            {"label": "SESSIONS", "title": "50-minute Session", "description": "Individual counseling, in person or telehealth.", "price_display": "$175", "cta_text": "Schedule", "cta_href": "/contact"},
            {"label": "EXTENDED", "title": "80-minute Session", "description": "Extended session for EMDR or intensive work.", "price_display": "$260", "cta_text": "Schedule", "cta_href": "/contact"},
            {"label": "SLIDING SCALE", "title": "Sliding-scale Session", "description": "Needs-based, no questions asked.", "price_display": "$90 – $140", "cta_text": "Inquire", "cta_href": "/contact"},
        ]
    if archetype_id == "fitness-studio":
        return [
            {"label": "YOGA", "title": "Vinyasa Flow", "description": "Heat-building, breath-linked movement.", "price_display": "$22 drop-in", "cta_text": "Book", "cta_href": "/contact"},
            {"label": "PILATES", "title": "Mat Pilates", "description": "Core, stability, control.", "price_display": "$22 drop-in", "cta_text": "Book", "cta_href": "/contact"},
            {"label": "RESTORE", "title": "Yin & Restorative", "description": "Long holds, deep rest.", "price_display": "$22 drop-in", "cta_text": "Book", "cta_href": "/contact"},
        ]
    if archetype_id == "salon-spa":
        return [
            {"label": "CUTS", "title": "Women's Cut + Style", "description": "Consultation, wash, cut, blow-dry.", "price_display": "from $85", "cta_text": "Book", "cta_href": "/contact"},
            {"label": "COLOR", "title": "Balayage", "description": "Hand-painted highlights + tone.", "price_display": "from $240", "cta_text": "Book", "cta_href": "/contact"},
            {"label": "BRIDAL", "title": "Bridal Styling", "description": "Trial + day-of styling.", "price_display": "from $500", "cta_text": "Inquire", "cta_href": "/contact"},
        ]
    if archetype_id == "restaurant-cafe":
        return [
            {"label": "BREAKFAST", "title": "Morning Menu", "description": "Served 7–11 AM.", "price_display": "$9 – $14", "cta_text": "See menu", "cta_href": "/menu"},
            {"label": "LUNCH", "title": "Lunch Menu", "description": "Served 11 AM – 2 PM.", "price_display": "$13 – $16", "cta_text": "See menu", "cta_href": "/menu"},
            {"label": "EVENING", "title": "Small Plates & Wine", "description": "Thu–Sat after 4 PM.", "price_display": "$8 – $22", "cta_text": "See menu", "cta_href": "/menu"},
        ]
    return []


def _normalize_harbor_services(raw) -> list:
    """Map harbor's `_harbor_meta.services` items
    ({title, description?, price?}) to the compose offerings-grid shape
    ({label, title, description, price_display, cta_text, cta_href}).

    Harbor specs come from `services/harbor_to_compose.ts` extracting
    Opus's offerings sections. Fields not present in harbor data are
    filled with neutral defaults so the resulting offerings render
    cleanly. Returns [] for empty / non-list input so callers can use
    `harbor_services or None` to fall through to archetype defaults.
    """
    import urllib.parse as _urlparse

    if not isinstance(raw, list):
        return []
    out: list = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        description = (item.get("description") or "").strip()
        price = (item.get("price") or "").strip()
        # No auto-derived eyebrow label: first-word-uppercased ("COVERAGE",
        # "VALVE", "DRIP") just restated the title and read as labelled
        # noise. Leave empty; widget conditionally renders.
        cta_query = _urlparse.quote_plus(title)[:80]
        # Don't default price_display to "Inquire" when no price; the CTA
        # already says "Inquire", so falling back to it produced two
        # identical labels per card. Empty + widget-side equality skip.
        out.append({
            "label": "",
            "title": title,
            "description": description,
            "price_display": price,
            "cta_text": "Inquire",
            "cta_href": f"/contact?service={cta_query}",
        })
    return out


_OFFERING_STYLE_MODEL_NAMES_INTAKE = {
    "session", "service", "offering", "offerings", "treatment",
    "treatments", "modality", "modalities", "package", "packages",
    "program", "programs", "lesson", "lessons", "course", "courses",
    "class",
}


def _services_from_offering_seed_rows(v1_spec) -> list:
    """Reconstruct the harbor services list from any offering-style
    data_model's seed_rows when _harbor_meta.services is empty.

    This is the systemic fallback for a services-extraction gap:
    when Opus's section vocabulary varies enough that
    harbor_to_compose's alias map doesn't capture the offerings block
    (e.g. content_hint vs content; novel section types per run), the
    keeper's prices still arrive in the data_model.seed_rows array
    (Opus follows the prompt's seed_rows instruction more reliably than
    the section type instruction). This fallback turns those rows into
    the same offerings shape _normalize_harbor_services produces.

    Reads from data_models whose name matches the offering-style noun
    set. Each seed_row is mapped via best-effort field matching:
    - title := name | title | label
    - description := description | summary | details
    - price := price_display | price (if string) | $X from price_cents
    """
    import urllib.parse as _urlparse

    if not isinstance(v1_spec, dict):
        return []
    models = v1_spec.get("data_models")
    if not isinstance(models, list):
        return []
    out: list = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip().lower()
        if name not in _OFFERING_STYLE_MODEL_NAMES_INTAKE:
            continue
        seed_rows = m.get("seed_rows")
        if not isinstance(seed_rows, list):
            continue
        for row in seed_rows:
            if not isinstance(row, dict):
                continue
            title = (row.get("title") or row.get("name") or row.get("label") or "").strip()
            if not title:
                continue
            description = (row.get("description") or row.get("summary") or row.get("details") or "").strip()
            price_display = (row.get("price_display") or "").strip()
            if not price_display:
                # Try string price field.
                price_str = row.get("price")
                if isinstance(price_str, str) and price_str.strip():
                    price_display = price_str.strip()
                else:
                    # Derive from price_cents (integer) when present.
                    cents = row.get("price_cents")
                    if isinstance(cents, int) and cents > 0:
                        # Show whole dollars when divisible by 100.
                        price_display = (
                            f"${cents // 100}" if cents % 100 == 0
                            else f"${cents / 100:.2f}"
                        )
            cta_query = _urlparse.quote_plus(title)[:80]
            # See `_normalize_harbor_services` for the rationale on the
            # empty `label` + non-defaulted `price_display`.
            out.append({
                "label": "",
                "title": title,
                "description": description,
                "price_display": price_display,
                "cta_text": "Inquire",
                "cta_href": f"/contact?service={cta_query}",
            })
        # Only consume the first matching offering model; subsequent
        # offering models would duplicate the offerings list.
        if out:
            break
    return out


def _normalize_harbor_testimonials(raw) -> list:
    """Map harbor's `_harbor_meta.testimonials` items ({quote, author?})
    to the compose testimonials shape ({quote, author_name, service}).

    Harbor specs carry verbatim keeper-mentioned quotes here. The
    compose shape requires `author_name` + `service`; we fill the gaps
    with neutral defaults so the keeper can rewrite later. The quote +
    author come straight from the keeper's conversation; description /
    title escaping is handled downstream by EJS `<%= %>`.
    """
    if not isinstance(raw, list):
        return []
    out: list = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        quote = (item.get("quote") or "").strip()
        if not quote:
            continue
        author = (item.get("author") or "").strip() or "Client (sample)"
        out.append({
            "quote": quote,
            "author_name": author,
            "service": "Client",
        })
    return out


_ARCHETYPE_DEFAULT_TESTIMONIALS: Dict[str, list] = {
    "event-producer": [
        {"quote": "A draft quote about a memorable event night. Replace with a real quote from an attendee, artist, or venue partner when you collect one.", "author_name": "Attendee (sample)", "service": "Past event"},
        {"quote": "Another draft pull-quote slot for a press mention, an artist review, or an attendee. Keep it short and specific; one concrete detail reads stronger than a general compliment.", "author_name": "Press mention (sample)", "service": "Coverage"},
        {"quote": "A third draft quote slot. Consider a quote from a partner venue or a returning attendee; replace this text as soon as you have a real one.", "author_name": "Partner venue (sample)", "service": "Collaboration"},
    ],
    "fitness-studio": [
        {"quote": "A draft member testimonial slot. Replace with a real quote about a class, instructor, or community moment when you have one.", "author_name": "Member (sample)", "service": "Member"},
        {"quote": "Another draft quote slot for a long-time member or a new one who stuck around. One concrete detail usually reads stronger than a general compliment.", "author_name": "Member (sample)", "service": "Member"},
        {"quote": "A third draft slot. A quote about the instructors, the community, or a personal milestone works well here.", "author_name": "Member (sample)", "service": "Member"},
    ],
    "restaurant-cafe": [
        {"quote": "A draft guest quote about a meal, the room, or a regular visit. Replace with a real pull-quote from a guest, a review, or a local write-up.", "author_name": "Guest (sample)", "service": "Dinner"},
        {"quote": "Another draft slot. A quote about a specific dish, the wine list, or a recurring night usually reads stronger than a general compliment.", "author_name": "Guest (sample)", "service": "Lunch"},
        {"quote": "A third draft slot. Press mentions and review excerpts fit here well; replace this text as soon as you have a real one.", "author_name": "Local press (sample)", "service": "Review"},
    ],
    "salon-studio": [
        {"quote": "A draft client quote about a cut, color, or consultation. Replace with a real quote as soon as a client agrees to be featured.", "author_name": "Client (sample)", "service": "Cut & color"},
        {"quote": "Another draft slot. A quote that names a stylist plus a specific service reads more specific than a generic compliment.", "author_name": "Client (sample)", "service": "Balayage"},
        {"quote": "A third draft slot. Consider a quote from a long-time client or a recent first-timer.", "author_name": "Client (sample)", "service": "Color"},
    ],
    "tradesperson-service": [
        {"quote": "A draft neighbor quote about a repair or install job. Replace with a real Google review excerpt or a direct client quote when you have one.", "author_name": "Neighbor (sample)", "service": "Repair"},
        {"quote": "Another draft slot. An after-hours call, a clean job site, or a fair quote usually reads stronger than a generic rating.", "author_name": "Neighbor (sample)", "service": "Install"},
        {"quote": "A third draft slot. Commercial clients, repeat customers, or property managers all belong here; replace this text as soon as a real quote is in hand.", "author_name": "Client (sample)", "service": "Service call"},
    ],
    "therapist-counselor": [
        {"quote": "A draft client testimonial slot. Replace with a real one only when you have explicit written consent; many therapists quote press or peer referrers instead.", "author_name": "Client (sample)", "service": "Therapy"},
        {"quote": "Another draft slot. Some practices use referral-source or peer-practitioner quotes here rather than direct client quotes, to respect client privacy.", "author_name": "Referral source (sample)", "service": "Referral"},
        {"quote": "A third draft slot. A CE instructor, supervisor, or published peer can also provide trust-building copy for this section.", "author_name": "Peer (sample)", "service": "Collegial"},
    ],
    "healer-wellness": [
        {"quote": "A draft client reflection slot. Replace with a real, consented quote about a session, a group program, or a transformation arc when you collect one.", "author_name": "Client (sample)", "service": "Session"},
        {"quote": "Another draft slot. A quote naming the modality (Reiki, breathwork, sound) plus a concrete shift usually reads stronger than a general compliment.", "author_name": "Client (sample)", "service": "Energy work"},
        {"quote": "A third draft slot. Consider a reflection from a long-term client or someone who attended a retreat or circle.", "author_name": "Client (sample)", "service": "Retreat"},
    ],
    "coach-creator": [
        {"quote": "A draft client quote about a coaching engagement. Replace with a real one: one concrete outcome plus the before-state tends to read strongest.", "author_name": "Client (sample)", "service": "Coaching"},
        {"quote": "Another draft slot. Executive sponsors, peer referrers, and workshop attendees can all provide testimonial copy here.", "author_name": "Client (sample)", "service": "Coaching"},
        {"quote": "A third draft slot. When a quote names the specific container (12-week, retainer, intensive) it reads more credible than a general compliment.", "author_name": "Client (sample)", "service": "Retainer"},
    ],
    "local-service": [
        {"quote": "A draft customer quote about a recent job. Replace with a real Google review excerpt or a direct client quote when you have permission to use one.", "author_name": "Customer (sample)", "service": "Service call"},
        {"quote": "Another draft slot. Specific outcomes (time saved, dollars saved, a surprise catch) read stronger than generic ratings.", "author_name": "Customer (sample)", "service": "Repair"},
        {"quote": "A third draft slot. Property managers, repeat customers, and commercial clients all belong here.", "author_name": "Customer (sample)", "service": "Install"},
    ],
}


def _default_testimonials(site_name: str, archetype_id: str = "coach-creator") -> list:
    tmpl = _ARCHETYPE_DEFAULT_TESTIMONIALS.get(archetype_id) or _ARCHETYPE_DEFAULT_TESTIMONIALS["coach-creator"]
    # First item weaves in the site_name so the scaffold feels slightly more
    # specific; other items stay fully neutral to avoid forcing a brand
    # voice customers may want to rewrite.
    out = [dict(tmpl[0])]
    out[0]["quote"] = tmpl[0]["quote"] + f" (on {site_name})" if "(on " not in tmpl[0]["quote"] else tmpl[0]["quote"]
    out.extend(dict(t) for t in tmpl[1:])
    return out


# 2026-04-22: archetype-curated placeholder hero/about images.
#
# Problem: the old fallback was `picsum.photos/seed/<app_name>-hero/1600/900`,
# which returns a TRUE-RANDOM photo from a landscape/nature pool. One
# wellness practitioner's build got a picture of a ferry
# crossing NY harbor with the Statue of Liberty. Zero wellness
# relevance. The admin-settings page lets the customer upload a real
# photo, but the first thing they or Ocean see is the placeholder, and
# an off-brand placeholder makes the whole build look careless.
#
# Fix: each archetype has a curated list of 4-6 picsum.photos stable
# IDs (picked from the 0-1084 range, skewed toward each archetype's
# visual vibe). Selection is deterministic from a hash of app_name so
# rebuilds of the same spec render consistently. Customer-supplied
# hero_image_url + about_photo_url always win; these only fire as
# fallbacks. Picsum IDs are stable per picsum.photos' contract (they
# don't rotate out without notice).

_ARCHETYPE_HERO_PICSUM_IDS: Dict[str, List[int]] = {
    # Soft nature + water + greenery for healers / therapists / spa
    "healer-wellness":       [1003, 1015, 1018, 1025, 1039, 1043],
    "therapist-counselor":   [1003, 1015, 1025, 1039, 1043, 1058],
    "salon-spa":             [1025, 1039, 1043, 1058, 434, 447],
    # Light open-air + movement for fitness
    "fitness-studio":        [1003, 1015, 1018, 416, 509, 685],
    # Warm tones + interiors + food for restaurants
    "restaurant-cafe":       [225, 312, 365, 429, 488, 534],
    # Professional / abstract / minimal for coaches
    "coach-consultant":      [20, 60, 164, 250, 309, 358],
    # Work / trades / industrial
    "tradesperson-contractor": [180, 215, 325, 398, 452, 481],
    "local-service":         [100, 200, 300, 400, 500, 600],
}

_ARCHETYPE_ABOUT_PICSUM_IDS: Dict[str, List[int]] = {
    # About/portrait slots: softer, more human / studio feel. These are
    # different from the hero pool so the two images do not duplicate.
    "healer-wellness":       [64, 65, 106, 140, 370, 541],
    "therapist-counselor":   [64, 65, 140, 190, 283, 370],
    "salon-spa":             [64, 65, 152, 306, 541, 634],
    "fitness-studio":        [146, 447, 509, 685, 822, 837],
    "restaurant-cafe":       [292, 365, 429, 488, 534, 787],
    "coach-consultant":      [20, 60, 250, 309, 358, 646],
    "tradesperson-contractor": [215, 325, 398, 452, 481, 610],
    "local-service":         [100, 200, 300, 400, 500, 600],
}


def _pick_picsum_id(archetype: str, app_name: str, pool: Dict[str, List[int]]) -> int:
    """Deterministically pick a curated picsum ID for this archetype.
    Same app_name → same image across rebuilds; different app_names
    spread across the pool so two builds of the same archetype don't
    trivially render the same placeholder."""
    ids = pool.get(archetype) or pool["local-service"]
    # Simple stable hash (sum-of-chars); no crypto needed; we just want
    # deterministic index selection with a bit of spread.
    h = sum(ord(c) for c in (app_name or "default"))
    return ids[h % len(ids)]


def _stable_index(seed: str, salt: str, modulo: int) -> int:
    """Deterministic non-crypto index in [0, modulo).

    Used by _rotate_suggestion + pick_layout_variant so two builds with
    the same `seed` (typically app_name) but different `salt` (the field
    being picked) get different rotation offsets. Without the salt, every
    field would lock-step on the same index and a healer with app_name
    "Maya" would always get suggestion[0] for palette AND font AND layout.

    Uses md5 (not Python hash() which is process-salted, not sum-of-chars
    which clusters badly on short strings). md5 is fine here: this is
    not a security boundary, just a stable, well-distributed integer.
    """
    if modulo <= 0:
        return 0
    import hashlib
    payload = f"{seed or 'default'}|{salt or ''}".encode("utf-8")
    digest = hashlib.md5(payload, usedforsecurity=False).digest()
    # Take first 8 bytes as little-endian unsigned int.
    return int.from_bytes(digest[:8], "little") % modulo


def _rotate_suggestion(suggestions: List[str], seed: str, salt: str) -> str:
    """Deterministically pick one item from a non-empty suggestion list.

    Same (suggestions, seed, salt) → same item, every call. Different
    salts spread different fields across the list so palette and font
    don't both lock to index 0 for a given app.
    """
    if not suggestions:
        raise ValueError("_rotate_suggestion requires a non-empty list")
    return suggestions[_stable_index(seed, salt, len(suggestions))]


_ARCHETYPE_SAMPLE_BLOG_POSTS: Dict[str, List[Dict[str, str]]] = {
    "healer-wellness": [
        {"title": "What to expect in your first session", "excerpt": "A short walkthrough of the opening arc, so you can arrive prepared.", "body": "The first session is mostly listening. We start with where you are, what brought you in, and what you want the work to hold. I will share how I work and answer any questions about the container before we begin. You can expect to leave with a sense of what the next few sessions might look like, and a small practice to carry between now and then.\n\nThis is placeholder copy. Replace with your own welcome when you are ready."},
        {"title": "On pacing, rest, and why slow often wins", "excerpt": "Why this work rarely moves as fast as people expect, and why that is usually the point.", "body": "Most people arrive wanting something to shift quickly. Sometimes it does. More often, the nervous system needs time to settle before anything deeper can happen, and the rush itself is part of what we are working with. Integration, the quieter days after a session, is where a lot of the real change lands.\n\nThis is placeholder copy. Edit freely in your voice."},
        {"title": "A few words on breath and attention", "excerpt": "How I think about the quieter parts of this work, and what clients can expect.", "body": "Most of what settles a busy mind is simple and repeatable. That is not a shortcut; it is the material we work with together. In practice, this looks less dramatic than people expect: slow breath, careful attention, and a willingness to notice what arrives.\n\nThis is placeholder copy. Replace with a post that describes your own modality."},
    ],
    "coach-consultant": [
        {"title": "How a twelve-week engagement actually unfolds", "excerpt": "A walk through the arc of a typical container, from kickoff to handoff.", "body": "The first two weeks are mostly mapping. We get clear on what you are actually trying to change, which is almost never the thing you named in the intake call. Weeks three through eight are where the work happens: targeted practices, concrete decisions, real consequences. The final month is about making the change durable without me in the room.\n\nThis is placeholder copy. Rewrite to fit how you run your engagements."},
        {"title": "The patterns I see most often", "excerpt": "Three or four things that come up on nearly every call, and what tends to move them.", "body": "Most of the operators I work with are not stuck on strategy. They are stuck on something smaller and harder: the specific conversation they keep postponing, the feedback they cannot quite hear, the decision they have already made but will not name. Naming the pattern is usually step one.\n\nThis is placeholder copy. Edit in your own voice."},
        {"title": "Measuring progress when the work is internal", "excerpt": "A few lightweight metrics I use to keep engagements honest.", "body": "Coaching outcomes resist easy measurement, which is why most coaching engagements drift. I use three simple check-ins: is the client making decisions faster, are the people around them reporting a difference, and is the specific pattern we named in week one showing up less. None of this is rigorous; all of it is useful.\n\nThis is placeholder copy. Replace with the metrics you actually care about."},
    ],
    "therapist-counselor": [
        {"title": "What to bring to our first appointment", "excerpt": "A short welcome note to help you arrive prepared.", "body": "Nothing formal. The first session is a conversation, not an intake form. If you want to write down a few things beforehand, what brought you in and what you hope to work on, that can help. Sessions run fifty minutes. We will leave a few minutes at the end to talk about whether to continue, and what a next step might look like.\n\nThis is placeholder copy. Replace with your own welcome when you are ready."},
        {"title": "Why fit matters more than credentials", "excerpt": "A short note on how I think about the therapeutic relationship.", "body": "The evidence is clear: the relationship between therapist and client predicts outcomes more than any specific modality. That does not mean credentials are irrelevant; it means that once you are in the room with a licensed clinician, the thing to pay attention to is whether you feel met. If you do not, say so. A good referral is often the best outcome of a first session.\n\nThis is placeholder copy. Edit to match your own philosophy."},
        {"title": "Confidentiality, explained simply", "excerpt": "What stays in the room, what does not, and why.", "body": "Everything you share in session stays between us, with three exceptions required by law: immediate risk to yourself, immediate risk to someone else, and ongoing harm to a vulnerable person. Those thresholds are narrower than people often assume. If I ever need to make a call under those limits, we will talk about it together first whenever that is possible.\n\nThis is placeholder copy. Confirm against your state's requirements before publishing."},
    ],
    "fitness-studio": [
        {"title": "A welcome for new members", "excerpt": "What your first two weeks will look like, and who to ask when you have questions.", "body": "Your first week is about getting comfortable: the space, the schedule, the people at the front desk. Come a few minutes early on your first class so we can show you where things are. Your second week is about finding two or three classes that feel like yours. The full schedule is available in the app and on the schedule page.\n\nThis is placeholder copy. Replace with your own welcome."},
        {"title": "How our programming shifts with the seasons", "excerpt": "Why the schedule looks different in January than it does in July, and what that feels like.", "body": "We program in twelve-week arcs that track the year. Fall and winter lean heavier, with more strength and less travel; spring and summer open up, with more mobility work and outdoor options. The arc is not a requirement; it is just how the schedule changes underneath if you keep showing up.\n\nThis is placeholder copy. Edit to match your programming."},
        {"title": "Members we're grateful for", "excerpt": "A spotlight on the people who make the community what it is.", "body": "One of the quiet privileges of running a studio is watching people grow over years, not weeks. This post is a placeholder for a short profile of a member whose journey might resonate with someone thinking about joining. Get their permission before publishing, keep it specific, and let their voice come through.\n\nThis is placeholder copy. Replace with a real member story when you have one."},
    ],
    "salon-spa": [
        {"title": "A note from the chair", "excerpt": "A short welcome and a bit of who we are.", "body": "We opened because we wanted a place that treated hair and skin the way we would want our own treated: unhurried, honest, and specific to you. Every appointment starts with a conversation about what you want, what your hair is doing right now, and what is realistic. The rest is our job.\n\nThis is placeholder copy. Rewrite in your own voice when you are ready."},
        {"title": "Caring for your look between visits", "excerpt": "A handful of small things that keep the work looking fresh for longer.", "body": "The biggest factor in how long a cut or color holds is what you do in the first week. Treat color as fragile for the first seven days: cool water, fewer washes, and a sulfate-free shampoo. Let a fresh cut dry naturally at least once before you style it. Small things, compounded.\n\nThis is placeholder copy. Replace with the aftercare you actually recommend."},
        {"title": "What we're loving this season", "excerpt": "A short editorial on the looks and treatments we are doing more of right now.", "body": "Every season has a quiet center of gravity: the colors that keep getting requested, the cuts that keep feeling right. This post is a placeholder for a short note on what we are loving in the studio this season, with a couple of photos and a line about who the look is for. Write one when the season asks for it.\n\nThis is placeholder copy. Replace with a seasonal feature."},
    ],
    "restaurant-cafe": [
        {"title": "This week from the kitchen", "excerpt": "What is coming off the menu, what is coming on, and where the ingredients are from.", "body": "We change the menu every few weeks, in small ways. This week we are rotating out the winter braise and bringing in a lighter spring preparation; the mushrooms are coming from a grower twenty miles north. If a dish you loved disappears, ask the team: we can usually tell you when it will be back.\n\nThis is placeholder copy. Replace with this week's actual changes."},
        {"title": "A few bottles we're opening right now", "excerpt": "Short tasting notes on a few wines we are excited about.", "body": "The list turns over slowly, but a few bottles are worth calling out. This post is a placeholder for a short note on two or three wines we are excited about, what they pair with on the current menu, and why we chose them. Keep it short and specific.\n\nThis is placeholder copy. Replace with a real wine feature."},
        {"title": "Events coming up this month", "excerpt": "Tasting menus, collaboration nights, and private events we are hosting soon.", "body": "A few nights this month we close the regular menu and do something specific: a tasting with a guest chef, a supper club, a wine dinner. This post is a placeholder for a list of what is coming up, with dates, tickets, and a line about what to expect at each one.\n\nThis is placeholder copy. Replace with real event listings."},
    ],
    "tradesperson-contractor": [
        {"title": "A recent job: what we found and how we fixed it", "excerpt": "A walk through a typical job, from first visit to final invoice.", "body": "A recent customer called us about a pressure issue in an older house. What looked like a simple fix turned out to involve a buried line that had corroded unevenly over twenty years. We walked them through the options, recommended the repair rather than the full replace, and finished in a day and a half.\n\nThis is placeholder copy. Replace with a real case study, with the customer's permission."},
        {"title": "What to do before we arrive", "excerpt": "Practical prep that saves time and gets you a better result.", "body": "A few small things before we show up save real time on the day. Clear a path to the work area, move anything fragile out of the room, and make sure someone with authority to make decisions is home. If pets are around, a closed room is usually easier than keeping an eye on them through the work.\n\nThis is placeholder copy. Edit to match how you run your jobs."},
        {"title": "Why licensing and insurance matter (and how to check)", "excerpt": "A short explainer on what the credentials cover, and how to verify them yourself.", "body": "Licensing exists because the work can cause real damage when done wrong, and insurance exists because even careful professionals occasionally cause real damage. Both are public records. In most states, a quick search of the contractor licensing board and a request for a certificate of insurance takes about ten minutes and is worth doing before anyone works on your property.\n\nThis is placeholder copy. Verify the specifics for your state before publishing."},
    ],
    "local-service": [
        {"title": "How we work, from first call to last invoice", "excerpt": "A short walkthrough of what a typical engagement looks like.", "body": "Most jobs start with a short call or form submission so we can understand what you need. We follow up with a quote, usually within a business day. If it is a fit, we schedule the visit, do the work, and invoice after. That is the simple version; every job has its own wrinkles, and we are happy to explain as we go.\n\nThis is placeholder copy. Replace with your own process."},
        {"title": "The questions customers ask most often", "excerpt": "A short FAQ drawn from what comes up on nearly every discovery call.", "body": "Three or four questions come up on almost every first call: how long will it take, what is it going to cost, what happens if something goes wrong, and who will actually show up. This post is a placeholder for short honest answers to those four questions, written the way you would say them on the phone.\n\nThis is placeholder copy. Answer in your own words."},
        {"title": "A recent project, start to finish", "excerpt": "A walk through a specific engagement so prospective customers can see what the work looks like.", "body": "This post is a placeholder for a short case study: what the customer needed, what we proposed, what we did, and how it turned out. Keep it specific enough that a prospective customer can see themselves in it, and short enough that they actually finish reading.\n\nThis is placeholder copy. Replace with a real project when you have one to share."},
    ],
}


_ARCHETYPE_SAMPLE_EVENT = {
    "healer-wellness": ("Evening circle: a sample event listing", "This is placeholder copy for an evening gathering. In your own listing, describe the container, what guests should bring, and what to expect when they arrive. Replace when you have a real event to run."),
    "coach-consultant": ("Cohort intro call: a sample event listing", "This is placeholder copy for a free intro call. In your own listing, describe the cohort format, who it is for, and what happens in the first two weeks. Replace when your next cohort opens."),
    "therapist-counselor": ("Community talk: a sample event listing", "This is placeholder copy for a public talk or workshop. In your own listing, describe the topic, who it is for, and what practical outcomes attendees can expect. Replace when ready."),
    "fitness-studio": ("Studio open house: a sample event listing", "This is placeholder copy for an open-house week. In your own listing, describe the free classes, the tour, and how to register. Replace with a real event when you have one."),
    "salon-spa": ("Seasonal launch night: a sample event listing", "This is placeholder copy for a seasonal product launch or pop-up. In your own listing, describe what is new, who will be there, and how to reserve a spot."),
    "restaurant-cafe": ("Tasting menu night: a sample event listing", "This is placeholder copy for a tasting-menu evening. In your own listing, describe the menu, the price, and how to book. Replace with a real date when you have one on the calendar."),
    "tradesperson-contractor": ("Free consultation week: a sample event listing", "This is placeholder copy for a free-consultation window. In your own listing, describe what the consultation covers, how long it lasts, and how customers can book a slot."),
    "local-service": ("Upcoming event: sample listing", "This is placeholder copy for an upcoming offering. Replace with real details when you have them."),
}


def _archetype_sample_blog_posts(archetype: str, app_name: str) -> list:
    """Archetype-appropriate placeholder blog posts with stable image URLs.
    Customer-visible on first build so titles + excerpts should read like
    real writing-in-progress, not `Sample Post N` stubs."""
    items = _ARCHETYPE_SAMPLE_BLOG_POSTS.get(archetype) or _ARCHETYPE_SAMPLE_BLOG_POSTS["local-service"]
    out = []
    for i, it in enumerate(items):
        out.append({
            "title": it["title"],
            "slug": f"sample-post-{i + 1}",
            "excerpt": it["excerpt"],
            "body": it["body"],
            "image_url": _seed_image(archetype, app_name, "blog", i, 1200, 800),
            "published": True,
            "published_at": f"2026-0{max(1, (i % 9) + 1)}-0{(i % 9) + 1}",
        })
    return out


def _archetype_sample_event_title(archetype: str) -> str:
    return _ARCHETYPE_SAMPLE_EVENT.get(archetype, _ARCHETYPE_SAMPLE_EVENT["local-service"])[0]


def _archetype_sample_event_description(archetype: str) -> str:
    return _ARCHETYPE_SAMPLE_EVENT.get(archetype, _ARCHETYPE_SAMPLE_EVENT["local-service"])[1]


def _event_producer_sample_upcoming(archetype: str, app_name: str) -> list:
    """Sample upcoming-events rows for the event-producer archetype, dated
    two to nine weeks after the day the spec is scaffolded (see
    compose.build._sample_date), so they are upcoming whenever it runs. Populates
    seed.events + home.upcoming_events.items so newly-scaffolded event-producer
    builds render a populated home page instead of "Nothing here yet."
    Four items is enough to fill the home-page resource-list-card-grid and
    give the events/index route real rows on first load. Copy is honest
    placeholder: titles read like a curator's calendar, bodies mark themselves
    as draft so the customer knows to edit."""
    return [
        {
            "title": "First Fridays: Gallery Night",
            "event_date": _sample_date(14),
            "event_time": "18:00",
            "end_time": "21:00",
            "location": "Downtown Arts District",
            "description": "A draft listing for a monthly gallery walk. Replace with your own program when you have dates confirmed, the galleries participating, and how visitors can plan a route.",
            "registration_url": "",
            "image_url": _seed_image(archetype, app_name, "event", 0, 1200, 800),
        },
        {
            "title": "Concert Series: Opening Night",
            "event_date": _sample_date(28),
            "event_time": "19:30",
            "end_time": "22:00",
            "location": "The Greenhouse Theater",
            "description": "A draft listing for the opening night of a four-week concert series. Replace with the actual lineup, the venue details, and ticketing link when you have them.",
            "registration_url": "",
            "image_url": _seed_image(archetype, app_name, "event", 1, 1200, 800),
        },
        {
            "title": "Art Fair: Early Registration",
            "event_date": _sample_date(49),
            "event_time": "10:00",
            "end_time": "18:00",
            "location": "Heritage Park",
            "description": "A draft listing for the annual summer art fair. Replace with participating artists, the day-of schedule, and any early-registration information for vendors or attendees.",
            "registration_url": "",
            "image_url": _seed_image(archetype, app_name, "event", 2, 1200, 800),
        },
        {
            "title": "Performance Workshop Series",
            "event_date": _sample_date(63),
            "event_time": "13:00",
            "end_time": "17:00",
            "location": "Studio B, 100 Example St",
            "description": "A draft listing for a weekend workshop series. Replace with the actual facilitators, the workshop topics, and how participants can register when you finalize the program.",
            "registration_url": "",
            "image_url": _seed_image(archetype, app_name, "event", 3, 1200, 800),
        },
    ]


def _seed_image(archetype: str, app_name: str, slot: str, index: int, width: int, height: int) -> str:
    """Stable-ID picsum URL for a seed-data image. Uses the archetype's
    curated pool; picks the ith entry (with wrap) plus a slot salt so
    different slot types (blog post / testimonial / service / staff)
    don't trivially collide on the same image. Stable across rebuilds
    because it only hashes archetype + slot + index, not app_name, so
    customer-facing sample images stay consistent. It
    replaces the old `picsum.photos/seed/<app_name>-<slot>-<i>` which
    returned TRUE-RANDOM photos that got blocked by some browsers +
    were off-brand for the archetype."""
    pool = _ARCHETYPE_HERO_PICSUM_IDS.get(archetype) or _ARCHETYPE_HERO_PICSUM_IDS["local-service"]
    slot_salt = sum(ord(c) for c in slot)
    pid = pool[(slot_salt + index) % len(pool)]
    return f"https://picsum.photos/id/{pid}/{width}/{height}"


def _default_hero_image_url(archetype: str, app_name: str, mood_text: str = "") -> str:
    """Archetype-curated hero image from the self-hosted stock library
    (cli/stock_photos/, copied into the build at /images/stock/). The
    customer's own hero_image_url (settings / _seed_settings) always
    overrides this. Returns '' (clean text-forward hero) when no curated
    photo matches; never picsum. Picsum shipped an off-brand logo-laden
    design-desk photo (id/20) onto a life coach's hero + about on
    2026-06-07; the curated library removes that class of surprise.
    `mood_text` (keeper description + mood_keywords) steers the broad
    coach-consultant archetype business-vs-wellness."""
    from compose.stock_photos import default_hero_image_url
    return default_hero_image_url(archetype, app_name, mood_text)


def _default_about_photo_url(archetype: str, app_name: str, mood_text: str = "") -> str:
    """Archetype-curated about image from the self-hosted stock library.
    Customer's own about_photo_url always overrides; '' when no match."""
    from compose.stock_photos import default_about_photo_url
    return default_about_photo_url(archetype, app_name, mood_text)


# Conventional max length for an HTML <meta name="description"> value.
# Most engines truncate visible search snippets near 155-160 chars; we use
# 155 as a safe ceiling and word-boundary the cut so it doesn't end mid-word.
_META_DESCRIPTION_MAX_LEN = 155


# ---------------------------------------------------------------------------
# Faithful-copy helpers (v2-faithful-site-copy-mode, 2026-05-08)
# ---------------------------------------------------------------------------

def _copy_path_to_page_id(path: str) -> str:
    """Convert a copy_pages absolute path to a compose page_id.

    Mapping:
      "/"                     -> "home"
      "/about"                -> "about"
      "/work-with-me"         -> "work-with-me"
      "/services/coaching"    -> "services-coaching"
      "/about.html"           -> "about-html"

    The page id becomes a route path and a view file name in the generated
    app, and the path comes from a crawl of somebody else's website, so
    the result is reduced to the slug alphabet compose.build accepts
    (lowercase letters, digits, "-", "_"; at most 64 characters). Anything
    else, a dot, a quote, "..", a percent escape, collapses to "-". A path
    with nothing usable in it maps to "page".
    """
    if not isinstance(path, str) or not path:
        return "home"
    p = path.strip().lower()
    if not p or p == "/":
        return "home"
    slug = re.sub(r"[^a-z0-9_]+", "-", p).strip("-_")[:64].strip("-_")
    return slug or "page"


def _build_copy_hero_block(
    hero_meta: dict,
    site_name: str,
    page_id: str,
) -> dict:
    """Translate a CopyPage.hero (TS shape) into the spec.pages.<id>.hero
    block consumed by the hero-centered widget. Defensive against
    missing keys; always returns a dict with at least a heading.
    """
    hero_meta = hero_meta or {}
    heading = hero_meta.get("heading")
    if not isinstance(heading, str) or not heading.strip():
        # Fall back to the page_id title-cased; safe default that beats
        # an empty hero on a faithful-copy import where the source page
        # had no <h1>.
        heading = (
            site_name
            if page_id == "home"
            else page_id.replace("-", " ").title()
        )
    block: dict = {"heading": heading}
    body_html = hero_meta.get("body_html")
    if isinstance(body_html, str) and body_html.strip():
        # The hero widget writes this value to the page unescaped. The
        # crawler is expected to hand over `<p>escaped text</p>` blocks,
        # but the value arrives inside a spec and nothing upstream of here
        # re-checks it, so it is reduced to the attribute-free tag
        # allowlist now. (compose.build applies the same function again
        # when it assembles the page; it is idempotent.)
        block["body_html"] = sanitize_html_fragment(body_html, max_len=8000)
    cred = hero_meta.get("credential_lines")
    if isinstance(cred, list):
        clean = [str(x).strip() for x in cred if isinstance(x, str) and x.strip()]
        if clean:
            block["credential_lines"] = clean
    sms = hero_meta.get("cta_text_me_phone")
    if isinstance(sms, str):
        digits = "".join(ch for ch in sms if ch.isdigit())
        if 7 <= len(digits) <= 15:
            block["cta_text_me_phone"] = digits
            block["cta_primary"] = {
                "text": "Text me",
                "href": f"sms:{digits}",
            }
    if "cta_primary" not in block:
        cta_text = hero_meta.get("cta_text")
        cta_href = hero_meta.get("cta_href")
        if isinstance(cta_text, str) and cta_text.strip() and isinstance(cta_href, str) and cta_href.strip():
            block["cta_primary"] = {
                "text": cta_text.strip()[:80],
                "href": cta_href.strip()[:600],
            }
    return block


def _truncate_meta_description(s: str, max_len: int = _META_DESCRIPTION_MAX_LEN) -> str:
    """Truncate a long string to a clean meta-description length on a word
    boundary. Returns "" for falsy input. The cut prefers the last space
    inside the budget so we never produce mid-word ellipsis. Adds an
    ellipsis only when truncation actually happened."""
    if not s:
        return ""
    s = " ".join(str(s).split())  # collapse whitespace runs
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    # Walk back to the last whitespace so we don't slice mid-word.
    space = cut.rfind(" ")
    if space >= int(max_len * 0.6):
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def scaffold_spec(answers: dict) -> dict:
    """Expand a short answers dict into a full v2 spec.

    Fills in archetype defaults: pages list, service/testimonial seeds,
    palette + font pair (from suggestions), page-level hero/description
    defaults, and site settings across all 24 canonical keys.
    """
    required = ("app_name", "archetype", "site_name", "admin_email")
    missing = [k for k in required if not answers.get(k)]
    if missing:
        raise ValueError(f"intake answers missing required: {', '.join(missing)}")

    archetype = get_archetype(answers["archetype"])
    palette = answers.get("palette") or _rotate_suggestion(
        archetype.get("default_palette_suggestions") or ["warm-earth"],
        answers["app_name"],
        "palette",
    )
    font_pair = answers.get("font_pair") or _rotate_suggestion(
        archetype.get("default_font_pair_suggestions") or ["rubik-nunito"],
        answers["app_name"],
        "font_pair",
    )
    site_name = answers["site_name"]
    hero_tagline = answers.get("hero_tagline") or archetype["copy_tone"].split(",")[0].strip().title()
    # hero_description is the short tagline-supporting paragraph below the
    # H1. It is OMITTED ENTIRELY when blank so the hero stays clean for
    # solo/minimal sites: we no longer auto-fill it with the long
    # archetype + site_name brief, which previously produced redundant
    # body copy that visually duplicated site_description (the SEO meta).
    # Customers can still set it via answers["hero_description"] / the
    # admin Settings form. site_description (SEO meta) carries the long
    # form below.
    hero_description = answers.get("hero_description") or ""
    site_description_default = f"{site_name}: {archetype['description']}"
    site_description = _truncate_meta_description(
        answers.get("site_description") or site_description_default
    )
    about_title = answers.get("about_title") or f"About {site_name}"
    about_body = answers.get("about_body") or f"{site_name} serves {archetype['copy_tone']}. Established with care and intention."
    contact_email = answers.get("contact_email") or answers["admin_email"]
    contact_phone = answers.get("contact_phone") or ""
    city_state = answers.get("city_state") or ""
    services = answers.get("services") or _default_services(answers["archetype"])
    testimonials = answers.get("testimonials") or _default_testimonials(site_name, answers["archetype"])
    social = answers.get("social") or {}

    # Mood signal for image selection: the keeper's own words (description +
    # mood_keywords) steer the broad coach-consultant archetype toward
    # professional vs wellness imagery (a finding of the automated build
    # review, 2026-06-07; that review loop ran outside this repository and
    # is not included in it).
    _hm_mood = []
    _hm = answers.get("_harbor_meta") if isinstance(answers.get("_harbor_meta"), dict) else {}
    if isinstance(_hm.get("mood_keywords"), list):
        _hm_mood = [str(x) for x in _hm["mood_keywords"]]
    _mood_text = " ".join([str(answers.get("site_description") or ""), str(site_description or ""), " ".join(_hm_mood)]).strip()
    hero_image_url = answers.get("hero_image_url") or _default_hero_image_url(answers["archetype"], answers["app_name"], _mood_text)
    about_photo_url = answers.get("about_photo_url") or _default_about_photo_url(answers["archetype"], answers["app_name"], _mood_text)
    nav_logo_url = answers.get("nav_logo_url") or ""

    settings = {
        "site_name": site_name,
        "site_description": site_description,
        "hero_tagline": hero_tagline,
        "hero_description": hero_description,
        "hero_image_url": hero_image_url,
        "nav_logo_url": nav_logo_url,
        "about_title": about_title,
        "about_body": about_body,
        "about_photo_url": about_photo_url,
        "contact_email": contact_email,
        "contact_phone": contact_phone,
        "contact_address": city_state,
        "business_name": answers.get("business_name") or site_name,
        "support_email": answers.get("support_email") or contact_email,
        "instagram_url": social.get("instagram_url", ""),
        "facebook_url": social.get("facebook_url", ""),
        "twitter_url": social.get("twitter_url", ""),
        "linkedin_url": social.get("linkedin_url", ""),
        "youtube_url": social.get("youtube_url", ""),
        "tiktok_url": social.get("tiktok_url", ""),
        "venmo_handle": answers.get("venmo_handle") or social.get("venmo_handle", ""),
    }

    # Pages: one hero per archetype default_page. Home is archetype-aware
    # because different archetypes' home layouts reference different widget
    # data sources (trust-badges, service-areas, team, instructors, etc).
    # Always emit a superset so every archetype's home layout resolves.
    pages: dict = {}
    offerings_block = {
        "eyebrow": "OFFERINGS",
        "heading": "What I Offer",
        "items": services,
    }
    testimonials_block = {
        "eyebrow": "VOICES",
        "heading": "What Clients Say",
        "items": testimonials,
    }
    cta_block = {
        "heading": "Ready to begin?",
        "description": "Reach out to start the conversation.",
        "cta_text": "Get in touch",
        "cta_href": "/contact",
    }
    newsletter_block = {
        "heading": "Stay in touch",
        "description": "Occasional dispatches, one email every few weeks.",
        "placeholder": "Your email",
        "button_text": "Subscribe",
    }
    # Statement-portrait hero data (consumed only by sibling layouts that
    # use the hero-two-col-statement widget; ignored by archetype-default
    # layouts that use hero-centered). When the keeper supplies these via
    # _seed_settings or _harbor_meta the new home layout can render their
    # actual brand-name H1 + credentials stack + multi-paragraph body +
    # sms-style CTA without falling back to fabricated copy. See
    # widgets/hero/two-col-statement.ejs data contract.
    _credentials_lines = answers.get("credentials_lines") or []
    _body_blocks = answers.get("home_body_blocks") or []
    _statement_phone_raw = answers.get("contact_phone") or ""
    _statement_phone_digits = "".join(ch for ch in _statement_phone_raw if ch.isdigit())
    _statement_cta = None
    _statement_cta_text = answers.get("statement_cta_text") or "Text me"
    if _statement_phone_digits:
        _statement_cta = {
            "text": _statement_cta_text,
            "href": f"sms:{_statement_phone_digits}",
            "icon": "message",
        }
    _statement_secondary = []
    for _slug in archetype.get("default_pages", []):
        if _slug in ("home", "service-areas", "reviews"):
            continue
        if _slug in ("services", "about", "contact"):
            _statement_secondary.append({
                "text": _slug.capitalize(),
                "href": f"/{_slug}",
            })
        if len(_statement_secondary) >= 3:
            break
    # When the keeper has a contact phone, prefer an sms-style primary CTA
    # over the generic "Get in touch" -> /contact button. Both the default
    # hero-centered widget and the new hero-two-col-statement widget read
    # cta_primary, so this single setting flows to every home layout.
    _resolved_cta_primary = _statement_cta or {"text": "Get in touch", "href": "/contact"}
    for page_id in archetype["default_pages"]:
        if page_id == "home":
            pages["home"] = {
                "hero": {
                    "eyebrow": site_name.upper(),
                    "heading": hero_tagline,
                    "description": hero_description,
                    "image_url": hero_image_url,
                    "image_alt": f"{site_name} photo",
                    "cta_primary": _resolved_cta_primary,
                    "cta_secondary": {"text": "Learn more", "href": "/about"},
                    # Fields consumed by hero-two-col-statement only. Harmless
                    # to other hero widgets (they ignore unknown keys).
                    "credentials_lines": list(_credentials_lines),
                    "body_blocks": list(_body_blocks),
                    "secondary_links": list(_statement_secondary),
                    "uppercase_heading": True,
                },
                # Both `offerings` and `services` aliases keyed so healer
                # + coach + local-service + tradesperson + fitness layouts
                # all find data under whichever name they reference.
                "offerings": offerings_block,
                "services": offerings_block,
                "testimonials": testimonials_block,
                "reviews": testimonials_block,
                "cta": cta_block,
                "newsletter": newsletter_block,
                # Tradesperson-specific blocks.
                # Trust badges intentionally seeded empty: the prior defaults
                # ("5★ Rating / Happy customers", "24/7 Emergency service",
                # "Family-Run / Your neighbors") were fabricated stats not tied
                # to the real business and violated content-integrity rules
                # (content-integrity rule). The trust-badges-row
                # widget skips rendering when items is empty, so removing these
                # leaves the home page truthful by default. Customers can add
                # real credentials via /admin/settings or a spec override.
                "trust": {"items": []},
                "areas": {"heading": "Service Areas", "items": [
                    {"city": city_state.split(",")[0].strip() if city_state else "Your city", "state": (city_state.split(",")[-1].strip() if "," in city_state else "")},
                ]},
                # Fitness-specific.
                "instructors": {"heading": "Meet Your Team", "items": []},
                # Salon-specific.
                "team": {"heading": "Meet Your Team", "items": []},
                # Restaurant-specific.
                "featured_dishes": offerings_block,
                "visit": {
                    "heading": "Visit",
                    "hours": [],
                    "address": city_state,
                    "phone": contact_phone,
                    "email": contact_email,
                },
                # Event-producer-specific: upcoming-events teaser on the
                # home page. Only populated when the archetype actually
                # uses it (the layout references spec.home.upcoming_events);
                # other archetypes carry the key but their layouts don't
                # reach it. Press testimonials alias the testimonials block.
                "upcoming_events": (
                    {
                        "eyebrow": "UPCOMING",
                        "heading": "What's Coming Up",
                        "items": [
                            {
                                "title": ev["title"],
                                "date": ev["event_date"],
                                "description": ev["description"],
                                "image_url": ev["image_url"],
                                "detail_href": "/events",
                            }
                            for ev in _event_producer_sample_upcoming(answers['archetype'], answers['app_name'])
                        ],
                    }
                    if answers['archetype'] == 'event-producer'
                    else {"items": []}
                ),
                "press": testimonials_block,
            }
        elif page_id == "about":
            pages["about"] = {
                "hero": {
                    "eyebrow": "ABOUT",
                    "heading": about_title,
                    "description": about_body,
                    "image_url": about_photo_url,
                    "image_alt": f"Portrait of {site_name}",
                },
                "cta": {
                    "heading": "Ready to work together?",
                    "description": "Reach out for an intro conversation.",
                    "cta_text": "Get in touch",
                    "cta_href": "/contact",
                },
            }
        elif page_id in ("services", "work-with-me"):
            _svc_image = answers.get("services_image_url") or ""
            _svc_intro = answers.get("services_intro") or ""
            _svc_hero = {
                "eyebrow": "SERVICES",
                "heading": "Services" if page_id == "services" else "Work With Me",
                "description": _svc_intro or "Every engagement is designed around what will actually move the needle.",
            }
            if _svc_image:
                _svc_hero["image_url"] = _svc_image
                _svc_hero["image_alt"] = f"{site_name} work"
            pages[page_id] = {
                "hero": _svc_hero,
                "offerings": {"heading": "Services", "items": services},
                "cta": {"heading": "Questions?", "description": "Let's talk.", "cta_text": "Contact", "cta_href": "/contact"},
                "services": {"heading": "Services", "items": services},
            }
        elif page_id == "contact":
            pages["contact"] = {
                "hero": {"eyebrow": "REACH OUT", "heading": "Get in Touch", "description": "I'd love to hear from you."},
                "form": {"heading": "Send a Message", "service_options": [s["title"] for s in services] + ["General Inquiry"]},
            }
        elif page_id == "blog":
            pages["blog"] = {"hero": {"eyebrow": "THINKING", "heading": "Blog", "description": "Ideas, reflections, field notes."}}
        elif page_id == "journal":
            pages["journal"] = {"hero": {"eyebrow": "REFLECTIONS", "heading": "Journal", "description": "Thoughts and notes."}}
        elif page_id == "events":
            events_page = {"hero": {"eyebrow": "GATHER", "heading": "Upcoming Events", "description": "Workshops, talks, and gatherings."}}
            if answers['archetype'] == 'event-producer':
                # Event-producer /events page is the canonical events
                # landing. Populate items so the resource-list-card-grid
                # on the spec side renders; runtime /events route still
                # reads from the events DB table (events/index.ejs).
                events_page["hero"]["heading"] = "Upcoming Events"
                events_page["hero"]["description"] = "The full calendar of gatherings, shows, and performances."
                events_page["items"] = [
                    {
                        "title": ev["title"],
                        "date": ev["event_date"],
                        "description": ev["description"],
                        "image_url": ev["image_url"],
                        "detail_href": "/events",
                    }
                    for ev in _event_producer_sample_upcoming(answers['archetype'], answers['app_name'])
                ]
            pages["events"] = events_page
        elif page_id == "past-events":
            pages["past-events"] = {
                "hero": {
                    "eyebrow": "ARCHIVE",
                    "heading": "Past Events",
                    "description": "A portfolio of recent productions.",
                },
                "items": [
                    {
                        "title": f"Past event {i+1}",
                        "image_url": _seed_image(answers['archetype'], answers['app_name'], 'event', i + 4, 1200, 800),
                    }
                    for i in range(6)
                ],
            }
        elif page_id == "gallery":
            pages["gallery"] = {"hero": {"eyebrow": "VISUAL", "heading": "Gallery", "description": "Moments from the work."}}
        elif page_id == "case-studies":
            pages["case-studies"] = {"hero": {"eyebrow": "PROOF", "heading": "Case Studies", "description": "Real engagements, real outcomes."}}
        elif page_id == "reviews":
            pages["reviews"] = {"hero": {"eyebrow": "REVIEWS", "heading": "What Clients Say"}, "items": testimonials}
        else:
            # Secondary pages get a simple hero.
            label = page_id.replace("-", " ").title()
            pages[page_id] = {"hero": {"eyebrow": label.upper(), "heading": label}}

    # Lead-style seed rows (contact_inquiries, newsletter_subscribers)
    # look like real customer activity in /admin/contact-inquiries and
    # /admin/newsletter-subscribers. On a real Harbor customer site that
    # is confusing (the customer thinks they have leads they don't have).
    # Gate them on an explicit opt-in flag for Harbor builds; non-Harbor
    # builds (CLI/legacy v1 fixtures with no _harbor_meta) keep the
    # placeholder behavior so the existing fixture pipeline stays intact.
    # Ungated, the three sample @example.com inquiries below would ship to a
    # keeper's admin inbox as leads they never received.
    _harbor_meta_answers = answers.get("_harbor_meta") if isinstance(answers.get("_harbor_meta"), dict) else None
    _is_harbor_build = "_harbor_meta" in answers and isinstance(answers.get("_harbor_meta"), dict)
    _seed_demo_optin = bool(
        answers.get("seed_demo_data")
        or (isinstance(_harbor_meta_answers, dict) and _harbor_meta_answers.get("seed_demo_data"))
    )
    _seed_lead_style_data = (not _is_harbor_build) or _seed_demo_optin

    _contact_inquiry_seed_rows = (
        [
            {"name": "Sample Lead", "email": "sample@example.com", "service_interest": (services[0]["title"] if services else "General Inquiry"), "message": "Just exploring: is this available in Q2?"},
            {"name": "Returning Customer", "email": "return@example.com", "service_interest": "", "message": "Had a great experience last year, reaching out again."},
            {"name": "New Referral", "email": "friend@example.com", "service_interest": "", "message": "A friend recommended you."},
        ]
        if _seed_lead_style_data
        else []
    )
    _newsletter_subscriber_seed_rows = (
        [
            {"email": "early.reader@example.com"},
            {"email": "new.friend@example.com"},
            {"email": "regular.subscriber@example.com"},
            {"email": "community.voice@example.com"},
        ]
        if _seed_lead_style_data
        else []
    )

    # Build seed_content with defaults for EVERY data_model the archetype
    # declares, so no table ships empty. Admin pagination + sort can be
    # validated on freshly-scaffolded builds.
    seed = {
        "testimonials": [
            {"author_name": t.get("author_name", "Anonymous"), "quote": t["quote"], "service": t.get("service", ""), "published": True}
            for t in testimonials
        ],
        "contact_inquiries": _contact_inquiry_seed_rows,
        "newsletter_subscribers": _newsletter_subscriber_seed_rows,
        "services": [
            {
                "title": s["title"],
                "description": s["description"],
                "price_display": s.get("price_display", ""),
                # Only seed stock photos when the keeper didn't supply their own services.
                # Harbor-extracted services are image-free by design. The
                # answers dict carries the meta under `_harbor_meta` (key
                # set in translate_v1_spec line ~1057); a prior version
                # checked `harbor_meta` without the underscore and silently
                # fell through to picsum stock-photo seeding for every
                # keeper-supplied service. Fixed 2026-05-06.
                **(
                    {"image_url": _seed_image(answers['archetype'], answers['app_name'], 'service', i, 800, 600)}
                    if not (answers.get("_harbor_meta") or {}).get("services")
                    else {}
                ),
            }
            for i, s in enumerate(services)
        ],
        # Reviews intentionally seeded empty: the prior defaults
        # ("D. Marlow / Highly recommend", "Priya S. / Best in town",
        # "Rob C. / Saved us from a flood", etc.) were fabricated quotes
        # attributed to invented customers. Even with believable initials,
        # those rows render on the public /reviews page (and on
        # local-service-home-reviews-rich) as if they were real Google-style
        # reviews from real people. Violates content-integrity rules
        # (content-integrity rule: "Real customer quotes
        # with consent, or nothing"). The reviews-grid widget skips
        # rendering when items is empty, so removing these leaves the
        # public surface truthful by default. Customers add real reviews
        # via /admin/reviews when they collect them.
        "reviews": [],
        "service_areas": [
            {"city": (city_state.split(",")[0].strip() if city_state else "Main City"), "state": (city_state.split(",")[-1].strip() if "," in city_state else ""), "zip_codes": "", "description": "Primary service area.", "sort_order": 1},
            {"city": "Nearby Town", "state": (city_state.split(",")[-1].strip() if "," in city_state else ""), "zip_codes": "", "description": "Same-day coverage.", "sort_order": 2},
            {"city": "North Suburb", "state": (city_state.split(",")[-1].strip() if "," in city_state else ""), "zip_codes": "", "description": "Next-day typically.", "sort_order": 3},
            {"city": "South Suburb", "state": (city_state.split(",")[-1].strip() if "," in city_state else ""), "zip_codes": "", "description": "Next-day typically.", "sort_order": 4},
        ],
        "photos": [
            {"title": f"Photo {i+1}", "caption": f"Sample photo #{i+1}", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'photo', i, 1200, 1200), "category": "general", "featured": (i < 3)}
            for i in range(8)
        ],
        "blog_posts": _archetype_sample_blog_posts(answers['archetype'], answers['app_name']),
        "events": (
            _event_producer_sample_upcoming(answers['archetype'], answers['app_name'])
            if answers['archetype'] == 'event-producer'
            else [
                {"title": _archetype_sample_event_title(answers['archetype']), "event_date": _sample_date(30), "event_time": "18:00", "end_time": "20:00", "location": city_state or "TBA", "description": _archetype_sample_event_description(answers['archetype']), "registration_url": "", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'event', 0, 1200, 800)},
            ]
        ),
        "past_events": [
            {
                "title": f"Previous production {i+1}",
                "event_date": f"2025-{(i % 12) + 1:02d}-15",
                "location": "Various venues",
                "description": "A draft archive entry. Replace with details from a real past production: the artists involved, the attendance, any press coverage or reviews.",
                "image_url": _seed_image(answers['archetype'], answers['app_name'], 'event', i + 4, 1200, 800),
            }
            for i in range(6)
        ],
        # Additional per-archetype seed blocks: scaffold defaults so
        # fitness/salon/therapist/restaurant archetypes all have data
        # in every table the archetype declares.
        # Staff intentionally seeded empty: prior defaults ("Alex Carter,
        # Lead" + "Morgan Rivers, Senior") were fabricated team members
        # rendered on the public /staff page and salon-home-* layouts as
        # if they were real employees. Content-integrity rule: no invented
        # people on customer-facing surfaces. The staff-card-grid widget
        # skips rendering when items is empty. Customers add their actual
        # team via /admin/staff.
        "staff": [],
        "class_types": [
            {"name": "Core Class", "description": "Our flagship class.", "duration_min": 60, "difficulty": "all-levels", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'class', 1, 800, 600)},
            {"name": "Advanced", "description": "For experienced practitioners.", "duration_min": 75, "difficulty": "intermediate", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'class', 2, 800, 600)},
            {"name": "Beginner-Friendly", "description": "Welcoming intro class.", "duration_min": 50, "difficulty": "beginner", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'class', 3, 800, 600)},
        ],
        # Instructors intentionally seeded empty: prior defaults ("Jordan
        # Blake, Lead Instructor" + "Sam Taylor, Instructor") were fabricated
        # team members rendered on the public /instructors page and
        # fitness-home-* layouts as if they were real staff. Content-
        # integrity rule: no invented people on customer-facing surfaces.
        # The instructor-grid widget skips rendering when items is empty.
        # Customers add their actual instructors via /admin/instructors.
        "instructors": [],
        # Schedule slots seeded empty: the prior defaults referenced the
        # fabricated instructors above ("Jordan", "Sam") and would render
        # on the public weekly-schedule-grid as if those were real classes.
        # Customers add their actual schedule via /admin/schedule-slots.
        "schedule_slots": [],
        "pricing_plans": [
            {"name": "Drop-In", "price_display": "$25", "billing_period": "session", "features": "Single session\nNo commitment", "cta_text": "Book", "cta_href": "/contact", "featured": False, "sort_order": 1},
            {"name": "Monthly Unlimited", "price_display": "$159", "billing_period": "month", "features": "Unlimited sessions\nMember-only events\nGuest passes", "cta_text": "Join", "cta_href": "/contact", "featured": True, "sort_order": 2},
            {"name": "Class Pack (10)", "price_display": "$195", "billing_period": "pack", "features": "10 sessions\n6-month expiry", "cta_text": "Buy", "cta_href": "/contact", "featured": False, "sort_order": 3},
        ],
        "faqs": [
            {"question": "How do I get started?", "answer": "Reach out via the contact form or give us a call. We'll schedule a quick intro conversation.", "category": "Getting Started", "sort_order": 1},
            {"question": "What do your sessions cost?", "answer": "See the pricing page for current rates. We also offer sliding-scale spots when available.", "category": "Fees", "sort_order": 2},
            {"question": "Do you offer virtual options?", "answer": "Yes, virtual sessions are available for most offerings.", "category": "Logistics", "sort_order": 3},
            {"question": "What's your cancellation policy?", "answer": "Cancellations with 24+ hours notice are free. Less than 24 hours is charged the full session fee.", "category": "Logistics", "sort_order": 4},
        ],
        "modalities": [
            {"name": "Approach A", "description": "Primary approach we work with.", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'modality', 1, 800, 600), "sort_order": 1},
            {"name": "Approach B", "description": "Secondary framework for deeper work.", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'modality', 2, 800, 600), "sort_order": 2},
            {"name": "Approach C", "description": "Supporting modality.", "image_url": _seed_image(answers['archetype'], answers['app_name'], 'modality', 3, 800, 600), "sort_order": 3},
        ],
        "menu_items": [
            {"name": "Signature Item", "description": "Our flagship offering.", "price_display": "$14", "section_name": "Mains", "dietary_tags": "", "available": True, "sort_order": 1},
            {"name": "House Salad", "description": "Seasonal greens + house vinaigrette.", "price_display": "$12", "section_name": "Mains", "dietary_tags": "V,GF", "available": True, "sort_order": 2},
            {"name": "Espresso", "description": "Locally roasted.", "price_display": "$4", "section_name": "Drinks", "dietary_tags": "V", "available": True, "sort_order": 3},
        ],
    }

    # Keep only the seed blocks for models this archetype actually declares; 
    # no point seeding staff/class_types into a healer build where those
    # tables don't exist.
    archetype_model_names = {m.get("plural") for m in archetype.get("default_data_models", [])}
    # Also map singular → plural aliases
    _archetype_table_aliases = {
        "testimonials", "contact_inquiries", "contact-inquiries",
        "newsletter_subscribers", "newsletter-subscribers",
        "services", "reviews", "service_areas", "service-areas",
        "photos", "blog_posts", "blog-posts", "events",
        "past_events", "past-events",
        "staff", "class_types", "class-types", "instructors",
        "schedule_slots", "schedule-slots", "pricing_plans", "pricing-plans",
        "faqs", "modalities", "menu_items", "menu-items",
    }
    kept_seed = {}
    for key, rows in seed.items():
        # Match either the underscore form (key) or the hyphen form.
        alt = key.replace("_", "-")
        if key in archetype_model_names or alt in archetype_model_names:
            kept_seed[key] = rows

    # Faithful-copy mode (v2-faithful-site-copy-mode, 2026-05-08).
    # When the keeper supplied an existing live website to mirror, the
    # CRAWL stage in services/site_copy_crawl.ts populated
    # _harbor_meta.copy_pages. For each entry we replace any matching
    # page in spec.pages with a faithful-copy block carrying:
    #   - hero (authored from the source URL's <h1> + opening paras)
    #   - _copy_blocks (verbatim markdown for the rest of the page)
    #   - _copy_source_url (audit trail)
    # The build stage detects _copy_blocks in resolve_spec, pre-renders
    # the markdown to safe HTML, and swaps in the copy-faithful-default
    # layout (hero-centered + content-raw-markdown). Non-archetype page
    # ids (e.g. /work-with-me on a tradesperson archetype) become entries
    # in spec.pages that resolve_spec promotes into page_layouts; they do
    # NOT need a custom_pages entry because page_layouts already drives
    # both route emission (emit_routes_pages_js) and view emission
    # (emit_page_view). Doubling up via custom_pages would produce two
    # GET handlers + two views.
    _copy_pages_data = answers.get("_harbor_meta")
    # _copy_path_to_page_id collapses path separators to dashes, so both
    # "/services/coaching" and "/services-coaching" map to "services-coaching"
    # and the second entry silently overwrites the first. We track seen ids
    # and surface duplicates as translation warnings instead of dropping the
    # earlier entry on the floor. The first occurrence wins (later duplicates
    # are skipped) so the SE reviewer can see the source paths involved.
    _copy_collision_warnings: list = []
    _copy_archetype_pages = set(archetype.get("default_pages") or [])
    # Paths the spec's own pages claim with a login. A copied page is
    # served without one at /<page id>, and compose.build refuses a spec in
    # which an auth_required page has a path that something else answers
    # publicly (`_auth_required_page_errors`). The copied pages can reach
    # this function after every other check ran (the web tier merges an
    # operator's crawl into `_harbor_meta` after its own translator), so
    # the contradiction is resolved here, toward less exposure: the
    # protected page stays and the copy is left out, with a warning.
    _protected_page_keys = {
        _path_key(_page["path"])
        for _page in (answers.get("custom_pages") or [])
        if isinstance(_page, dict) and (_page.get("auth_required") or _roles_imply_login(_page))
        and isinstance(_page.get("path"), str) and _page["path"].strip()
    }
    if isinstance(_copy_pages_data, dict):
        _raw_copy_pages = _copy_pages_data.get("copy_pages")
        if isinstance(_raw_copy_pages, list) and _raw_copy_pages:
            _seen_copy_page_ids: dict = {}
            for _cp in _raw_copy_pages:
                if not isinstance(_cp, dict):
                    continue
                _path = _cp.get("path") or "/"
                _blocks = _cp.get("markdown_blocks") or []
                if not isinstance(_blocks, list) or not _blocks:
                    continue
                _hero = _cp.get("hero") if isinstance(_cp.get("hero"), dict) else {}
                _page_id = _copy_path_to_page_id(_path)
                _reserved_because = _page_id_reservation(
                    _page_id, _copy_archetype_pages
                )
                if _reserved_because:
                    # The crawled site has a page at a path the generated
                    # app serves itself (/login, /privacy, /admin, ...).
                    # compose.build refuses such a page id outright, because
                    # its view would replace the built-in one, so the copy
                    # is left out here and the reviewer is told.
                    _copy_collision_warnings.append({
                        "reason": "copy_pages_reserved_page_id",
                        "detail": (
                            f"copy_pages entry {str(_path)[:80]!r} maps to page_id "
                            f"{_page_id!r}, which {_reserved_because}; the "
                            "copied page was not imported"
                        ),
                    })
                    continue
                _copy_route = "/" if _page_id == "home" else f"/{_page_id}"
                if _copy_route in _protected_page_keys:
                    _copy_collision_warnings.append({
                        "reason": "copy_pages_protected_path",
                        "detail": (
                            f"copy_pages entry {str(_path)[:80]!r} would be served "
                            f"without a login at {_copy_route!r}, where the spec has a "
                            "page marked auth_required; the copied page was not imported"
                        ),
                    })
                    continue
                if _page_id in _seen_copy_page_ids:
                    _copy_collision_warnings.append({
                        "reason": "copy_pages_page_id_collision",
                        "detail": (
                            f"copy_pages entries {_seen_copy_page_ids[_page_id]!r} "
                            f"and {_path!r} both map to page_id {_page_id!r}; "
                            f"first occurrence kept, later entry dropped"
                        ),
                    })
                    continue
                _seen_copy_page_ids[_page_id] = _path
                _hero_block = _build_copy_hero_block(_hero, site_name, _page_id)
                pages[_page_id] = {
                    "hero": _hero_block,
                    "_copy_blocks": list(_blocks),
                    "_copy_source_url": (
                        _copy_pages_data.get("copy_source_url") or ""
                    ),
                }

    spec = {
        "app_name": answers["app_name"],
        "archetype": answers["archetype"],
        "palette": palette,
        "font_pair": font_pair,
        "admin_email": answers["admin_email"],
        "settings": settings,
        "pages": pages,
        "seed_content": kept_seed,
    }

    # When the customer provided data_models via intake answers (typically
    # from translate_v1_spec), surface them on the output spec so
    # compose.build.build_app picks them up. Without this, build_app falls
    # back to archetype["default_data_models"] and the customer's tables
    # are silently dropped. Explicit empty list from caller is treated as
    # "defer to archetype defaults" (same as missing key).
    answer_models = answers.get("data_models")
    if answer_models:
        spec["data_models"] = answer_models

    # Customer-declared non-archetype pages. Passed
    # through as-is; build.py:_custom_pages_filter drops collisions at
    # emit time. Empty list is treated as "no customs" (key omitted).
    answer_custom_pages = answers.get("custom_pages")
    if answer_custom_pages:
        spec["custom_pages"] = answer_custom_pages

    # Customer-declared user roles. Emitted as a
    # documentation comment in seed.sql by compose.build.emit_seed_sql;
    # SE reviewers + admins see the role catalog at a glance.
    answer_user_roles = answers.get("user_roles")
    if answer_user_roles:
        spec["user_roles"] = answer_user_roles

    # Translation warnings metadata. build_app writes each warning into
    # the build report (compose-manifest.txt, translation-warnings.txt),
    # e.g. "dropped /foo: path missing leading slash".
    warnings = answers.get("_translation_warnings")
    # Merge in any copy_pages page_id collisions detected above. The
    # translator (translate_v1_spec) typically populates the inbound
    # warnings list; CLI callers without a translator pass none. Either
    # way, collisions surface on the output spec so SE reviewers see them.
    if _copy_collision_warnings:
        if isinstance(warnings, list):
            warnings = list(warnings) + _copy_collision_warnings
        else:
            warnings = list(_copy_collision_warnings)
    if warnings:
        spec["_translation_warnings"] = warnings

    # Carry harbor_meta through to BuildSpec so the
    # build stage can call pick_layout_variant. Translator passes it via
    # answers["_harbor_meta"]; CLI callers without harbor context omit it.
    harbor_meta = answers.get("_harbor_meta")
    if isinstance(harbor_meta, dict) and harbor_meta:
        spec["_harbor_meta"] = harbor_meta

    # Pick a template family
    # deterministically from (archetype, harbor_meta, app_name) and stamp
    # it on the spec. Always non-empty (defaults to 'classic-business').
    # CLI callers that pass an explicit `template_family_id` in answers
    # win over the picker.
    spec["template_family_id"] = (
        answers.get("template_family_id")
        or pick_template_family(answers["archetype"], harbor_meta, seed=answers["app_name"])
    )

    return spec


# ---------------------------------------------------------------------------
# v1 → v2 translator
# ---------------------------------------------------------------------------

# Keyword → archetype classifier. Ordered by specificity: the first
# archetype whose keyword set hits gets picked. Fall-through is
# local-service (generic professional).
_ARCHETYPE_KEYWORDS = [
    ("therapist-counselor", [
        "therapist", "counselor", "counseling", "lcsw", "psychologist",
        "psychiatrist", "mental health", "psychotherapy", "licensed clinical",
        "emdr", "ifs", "internal family systems",
    ]),
    ("fitness-studio", [
        # Movement / yoga / pilates studios
        "yoga", "pilates", "barre", "crossfit", "fitness studio",
        "personal trainer", "class schedule", "membership pass",
        # Stretch and mobility: without these a stretch studio was
        # classified as local-service.
        "stretch studio", "stretch class", "stretching", "mobility",
        "mobility class",
        # Dance, martial arts, and other group-class movement studios
        "dance studio", "ballet", "dance class",
        "martial arts", "kickboxing", "boxing gym", "muay thai",
        # Gyms + strength training
        "gym", "gym membership", "strength training", "weightlifting",
        # Spin / cycling + specialized cardio studios
        "spin class", "spin studio", "cycling studio", "indoor cycling",
        "cardio class",
    ]),
    ("salon-spa", [
        "salon", "spa", "stylist", "hairdresser", "hair salon",
        "nail salon", "esthetician", "esthetics", "massage therapist",
        "bridal", "balayage", "colorist",
    ]),
    ("restaurant-cafe", [
        "restaurant", "cafe", "café", "coffee shop", "bakery",
        "menu", "chef", "kitchen", "tasting", "wine bar", "catering",
    ]),
    ("tradesperson-contractor", [
        "plumber", "plumbing", "electrician", "electrical", "hvac",
        "handyman", "roofer", "contractor", "general contractor",
        "licensed contractor", "emergency service",
    ]),
    # Event producers, curators, festival organizers. Ordered BEFORE
    # healer-wellness + coach-consultant because generic terms like "event"
    # leak into those archetypes via data-model token expansion (sample-healer
    # carries an Event model). Keywords here are deliberately multi-word so
    # single-word "event" in a different archetype does not accidentally
    # pull the spec into event-producer classification.
    ("event-producer", [
        "art show", "art fair", "art walk", "art producer",
        "gallery exhibition", "exhibition opening", "open studios",
        "event producer", "events producer", "event production",
        "event curator", "artistic director",
        "concert promoter", "concert series", "concert production",
        "music festival", "festival organizer", "festival production",
        "performance series", "performing arts series", "performing arts",
        "pop-up show", "pop up show",
        "event booking agency", "art booking",
    ]),
    ("healer-wellness", [
        "healer", "energy work", "reiki", "hypnotherapy",
        "hypnotherapist", "hypnosis", "subconscious work", "sound bath",
        "crystal", "breathwork", "intuitive", "spiritual", "shamanic",
        "akashic", "chakra",
    ]),
    # Fiction authors, novelists, indie writers. Ordered BEFORE
    # coach-consultant because terms like "writer" / "memoir" overlap
    # with coaching content marketing, and authors are a distinct
    # archetype (book inventory + retailer routing, not 1:1 sessions).
    # Added 2026-05-14 (v2-author-archetype-books-data-model); driver was
    # a hand-tuned coach-consultant build for an author (the canonical
    # author reference).
    ("author", [
        "novelist", "novel",
        "fiction author", "fiction writer",
        "indie author", "self-published author",
        "memoirist", "memoir",
        "poet", "poetry collection",
        "literary fiction", "speculative fiction",
        "science fiction author", "fantasy author",
        "thriller author", "mystery author",
        "romance author", "young adult author", "ya author",
        "book series", "trilogy",
        "audiobook", "ebook",
        "amazon kdp", "indie bookstore", "barnes & noble", "netgalley", "goodreads",
        "author website", "writer website",
    ]),
    ("coach-consultant", [
        "coach", "coaching", "consultant", "consulting",
        "executive coach", "business coach", "life coach",
        "strategy", "leadership", "mentor",
        # Private instructors and one-on-one teaching practices. Without
        # these a private-lesson business was classified as local-service
        # and inherited its default services. Coach-consultant's program /
        # session / mentor structure maps to private-lesson businesses far
        # better than local-service.
        "music teacher", "music instructor", "music lessons", "music school",
        "music studio", "voice teacher", "vocal coach",
        "piano teacher", "piano lessons", "piano studio",
        "guitar teacher", "guitar lessons", "guitar instructor",
        "violin teacher", "violin lessons",
        "drum teacher", "drum lessons",
        "voice lessons", "singing lessons", "singing teacher",
        "private lessons", "private tutor", "tutoring", "tutor",
        "language tutor", "language teacher",
        "sat prep", "act prep", "test prep",
    ]),
]


# Set of archetype ids the classifier can return; built from the keyword
# table plus the local-service fallback. Used to validate the harbor
# `_harbor_meta.archetype_hint` field before honoring it as a strong
# signal in detect_archetype.
_KNOWN_ARCHETYPE_IDS = {a for a, _ in _ARCHETYPE_KEYWORDS} | {"local-service"}


def detect_archetype(v1_spec: dict) -> str:
    """Classify a v1 AppSpec's archetype via keyword match against the
    description + data model names + action names. Falls back to
    local-service for anything that doesn't match a specific archetype.

    Harbor-extracted specs (services/harbor_to_compose.ts) carry an
    archetype_hint in spec._harbor_meta when Opus's archetype guess
    matched one of the 10 known archetype ids. Opus reads the entire
    keeper memory + last 30 turns, so its guess is much higher signal
    than substring text matching against a few keywords. Honor the hint
    when set + valid; fall through to keyword matching when absent or
    the hint is not a known archetype id (defensive: TS already
    validates against KNOWN_ARCHETYPES, but a future alternative
    extractor could pass an unknown value here).
    """
    harbor_meta = v1_spec.get("_harbor_meta")
    if isinstance(harbor_meta, dict):
        hint = harbor_meta.get("archetype_hint")
        if isinstance(hint, str) and hint in _KNOWN_ARCHETYPE_IDS:
            return hint

    import re as _re_split

    # Split CamelCase / snake_case / kebab-case model + action names into
    # space-separated words so word-boundary matching picks them up
    # (e.g. "PlumbingJob" → "Plumbing Job"; "contact_message" → "contact
    # message"; "service-area" → "service area").
    def _tokenize(s):
        if not s:
            return ""
        s1 = _re_split.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(s))
        return s1.replace("_", " ").replace("-", " ")

    text_parts = [str(v1_spec.get("description", "") or "")]
    # Defensive .get chains: every v1_spec list may contain non-dict
    # entries (empty slots, None, strings) from partially-validated
    # drafts. Skip those rather than crash the classifier.
    for dm in v1_spec.get("data_models") or []:
        if isinstance(dm, dict):
            text_parts.append(_tokenize(dm.get("name", "")))
    for a in v1_spec.get("actions") or []:
        if isinstance(a, dict):
            text_parts.append(_tokenize(a.get("name", "")))
            text_parts.append(str(a.get("description", "")))
    for r in v1_spec.get("user_roles") or []:
        if isinstance(r, dict):
            text_parts.append(_tokenize(r.get("role", "")))
            text_parts.append(str(r.get("description", "")))
    for p in v1_spec.get("pages") or []:
        # Defensive: skip non-dict entries so callers can pass partially-
        # validated v1 specs (e.g. intake drafts mid-conversation) without
        # crashing the classifier.
        if not isinstance(p, dict):
            continue
        text_parts.append(str(p.get("title", "")))
        text_parts.append(str(p.get("purpose", "")))
    haystack = " ".join(text_parts).lower()
    # Word-boundary match so generic keywords like "therapist" do not
    # substring-match inside "hypnotherapist", "aromatherapist", etc.
    # (surfaced when a hypnotherapist's site was
    # misclassified as therapist-counselor because
    # "therapist" lives inside "hypnotherapist"). Keyword-by-keyword
    # regex so multi-word phrases ("sound bath", "energy work") still
    # match as phrases.
    import re as _re
    for archetype_id, keywords in _ARCHETYPE_KEYWORDS:
        for kw in keywords:
            # \b at each end: matches word transitions. For keywords
            # starting/ending with non-word chars (none today, but
            # defensive), \b degrades to empty-match which is fine.
            if _re.search(r"\b" + _re.escape(kw) + r"\b", haystack):
                return archetype_id
    return "local-service"


def _harbor_meta_hint(v1_spec: dict, key: str) -> Optional[str]:
    """Read a string-typed hint from `_harbor_meta` if present; else None.

    Pure helper. Does NOT validate against any registry; callers do
    that with `_validate_registry_id`. Returns the trimmed lowercase
    string or None on missing/non-string/empty.
    """
    meta = v1_spec.get("_harbor_meta")
    if not isinstance(meta, dict):
        return None
    raw = meta.get(key)
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    return v or None


def _harbor_meta_keyword_list(v1_spec: dict, key: str) -> List[str]:
    """Read a string[] hint from `_harbor_meta` (already normalized by
    harbor_to_compose.ts to lowercase + capped at 5). Returns [] on
    missing/wrong shape so the scorer can iterate without nil checks.
    """
    meta = v1_spec.get("_harbor_meta")
    if not isinstance(meta, dict):
        return []
    raw = meta.get(key)
    if not isinstance(raw, list):
        return []
    return [str(x).strip().lower() for x in raw if isinstance(x, str) and x.strip()]


# ============================================================
# Layout variant scorer
# ============================================================
# Pure function: same input → same output. Mirrors the deterministic
# contract of detect_archetype + _rotate_suggestion. Selects ONE of the
# sibling layouts in the same `variant_group` based on harbor_meta hints
# + tag intersections; falls back deterministically when signal is thin.
#
# Anti-goal: no LLM, no nondeterminism, no per-call IO beyond loading
# the static layouts JSON (cached). v2's fundamental contract.
#
# Score components (per candidate layout):
#   +5 if `layout_variant_hint` matches the layout's full key
#   +3 per overlap word between (mood_keywords ∪ {density_hint, hero_style_hint})
#      and (layout.tags ∪ {layout.mood, layout.density, layout.hero_style})
#   −5 per overlap with avoid_keywords against the same target set
#   tiebreak: deterministic _stable_index over remaining top-scored
#             candidates seeded by (seed, "layout-variant:" + group)


def _candidate_layouts_for(archetype: str, page: str) -> List[dict]:
    """Return all layouts in `compose/data/page-layouts.json` matching
    archetype + page. Includes both legacy single layouts (no
    variant_group) and grouped variants (with variant_group set).

    Imported lazily because `compose.build` owns layout loading and
    importing at module top would create a cycle (build → intake).
    """
    from compose.build import load_layouts as _load_layouts
    layouts_doc = _load_layouts()
    layouts = layouts_doc.get("layouts", {}) if isinstance(layouts_doc, dict) else {}
    out: List[dict] = []
    for key, layout in layouts.items():
        if not isinstance(layout, dict):
            continue
        if layout.get("archetype") != archetype:
            continue
        if layout.get("page") != page:
            continue
        # Carry the canonical key on the dict so the caller can return it.
        out.append({**layout, "_key": key})
    return out


def _score_layout_against_meta(layout: dict, harbor_meta: dict) -> int:
    """Score a single layout against design hints. Higher = better fit.

    See module-level comment for component weights.
    """
    if not isinstance(harbor_meta, dict):
        harbor_meta = {}

    # Signal vocabulary derived from the layout itself.
    layout_terms: set = set()
    for t in layout.get("tags") or []:
        if isinstance(t, str):
            layout_terms.add(t.strip().lower())
    for k in ("mood", "density", "hero_style", "variant_id"):
        v = layout.get(k)
        if isinstance(v, str) and v:
            layout_terms.add(v.strip().lower())

    # Signal vocabulary derived from the keeper's hints.
    keeper_terms: set = set()
    for t in harbor_meta.get("mood_keywords") or []:
        if isinstance(t, str):
            keeper_terms.add(t.strip().lower())
    for k in ("density_hint", "hero_style_hint"):
        v = harbor_meta.get(k)
        if isinstance(v, str) and v:
            keeper_terms.add(v.strip().lower())

    avoid_terms: set = set()
    for t in harbor_meta.get("avoid_keywords") or []:
        if isinstance(t, str):
            avoid_terms.add(t.strip().lower())

    score = 0
    score += 3 * len(keeper_terms & layout_terms)
    score -= 5 * len(avoid_terms & layout_terms)
    return score


def _candidate_template_families(archetype: str) -> List[dict]:
    """Return all template families compatible with the given archetype.

    A family with `compatible_archetypes` == ['*'] (wildcard) is
    universal. Otherwise only families that explicitly list the
    archetype id are eligible.
    """
    from compose.build import load_templates as _load_templates
    families = _load_templates().get("templates", []) if isinstance(_load_templates(), dict) else []
    out: List[dict] = []
    for f in families:
        if not isinstance(f, dict):
            continue
        compat = f.get("compatible_archetypes") or []
        if compat == ["*"] or archetype in compat:
            out.append(f)
    return out


def _score_template_against_meta(family: dict, harbor_meta: dict) -> int:
    """Score a template family against design hints. Higher = better.

    Scoring components:
      +5 if `complexity_hint` matches `family.complexity_band`
      +3 per overlap word between
            (mood_keywords ∪ {hero_style_hint, density_hint})
            and (family.tags ∪ {family.complexity_band, family.accent_density})
      -5 per overlap with avoid_keywords against the same target set
    """
    if not isinstance(harbor_meta, dict):
        harbor_meta = {}
    family_terms: set = set()
    for t in family.get("tags") or []:
        if isinstance(t, str):
            family_terms.add(t.strip().lower())
    for k in ("complexity_band", "accent_density"):
        v = family.get(k)
        if isinstance(v, str) and v:
            family_terms.add(v.strip().lower())

    keeper_terms: set = set()
    for t in harbor_meta.get("mood_keywords") or []:
        if isinstance(t, str):
            keeper_terms.add(t.strip().lower())
    for k in ("hero_style_hint", "density_hint"):
        v = harbor_meta.get(k)
        if isinstance(v, str) and v:
            keeper_terms.add(v.strip().lower())

    avoid_terms: set = set()
    for t in harbor_meta.get("avoid_keywords") or []:
        if isinstance(t, str):
            avoid_terms.add(t.strip().lower())

    score = 0
    score += 3 * len(keeper_terms & family_terms)
    score -= 5 * len(avoid_terms & family_terms)

    complexity = harbor_meta.get("complexity_hint")
    if isinstance(complexity, str) and complexity.strip().lower() == (family.get("complexity_band") or "").strip().lower():
        score += 5

    return score


def pick_template_family(
    archetype: str,
    harbor_meta: Optional[dict],
    seed: str = "default",
) -> str:
    """Deterministically pick a template family id for an archetype.

    Returns the family id string. ALWAYS returns a value (never None) so
    callers can stamp BuildSpec.template_family_id without nil checks.
    Falls back to "classic-business" when no candidates match the
    archetype (defensive against future archetypes added without an
    explicit family compatibility entry).

    Contract: same (archetype, harbor_meta, seed) -> same family id,
    every call. Pure function. Mirrors pick_layout_variant.

    Honors:
      1. `_harbor_meta.template_family_hint` (strongest signal) when it
         names a real, archetype-compatible family.
      2. Otherwise scores candidates and picks the top scorer.
      3. Tie-break with deterministic md5 over seed.
    """
    candidates = _candidate_template_families(archetype)
    if not candidates:
        return "classic-business"

    meta = harbor_meta if isinstance(harbor_meta, dict) else {}

    # Strongest signal: explicit hint that names an archetype-compatible family.
    raw_hint = meta.get("template_family_hint")
    if isinstance(raw_hint, str):
        hint = raw_hint.strip().lower()
        if hint:
            for c in candidates:
                if (c.get("id") or "").lower() == hint:
                    return c["id"]

    # Score remainder.
    scored = [(c, _score_template_against_meta(c, meta)) for c in candidates]
    top_score = max(s for _, s in scored)
    top = [c for c, s in scored if s == top_score]

    if len(top) == 1:
        return top[0]["id"]

    # Tie-break: when scoring is all-zero (no signal), prefer the
    # family explicitly tagged "default" so customers without
    # expressed preference get a stable, predictable shell. This is
    # different from layout-variant rotation: layout variants want
    # to spread across siblings (more visible variety in the gallery);
    # template families want stability when there's no signal (the
    # CHROME of a site is more disorienting to vary than the page
    # layout). Customers signal a preference -> they get the variety.
    if top_score == 0:
        defaults = [c for c in top if "default" in (c.get("tags") or [])]
        if len(defaults) == 1:
            return defaults[0]["id"]

    top_sorted = sorted(top, key=lambda c: c["id"])
    idx = _stable_index(seed, f"template-family:{archetype}", len(top_sorted))
    return top_sorted[idx]["id"]


def pick_layout_variant(
    archetype: str,
    page: str,
    harbor_meta: Optional[dict],
    seed: str = "default",
) -> Optional[str]:
    """Deterministically pick a layout key for (archetype, page) using
    harbor_meta hints. Returns the layout's canonical key (e.g.
    "healer-home-trust-first") or None if no matching layout exists.

    Contract:
      - Pure function: same (archetype, page, harbor_meta, seed) →
        same layout key, every call.
      - Honors an exact `_harbor_meta.layout_variant_hint` if it names
        a real layout in the candidate set. This is the strongest
        signal: an explicit choice recorded for the keeper.
      - Otherwise scores candidates and picks the top scorer; ties
        broken by _stable_index over the seed.
      - When no candidates exist (caller passed an unknown archetype
        or page), returns None; caller may fall back to its own
        default key.
    """
    candidates = _candidate_layouts_for(archetype, page)
    if not candidates:
        return None

    meta = harbor_meta if isinstance(harbor_meta, dict) else {}

    # Strongest signal: exact hint that names a real candidate.
    # Two accepted forms: full layout key (e.g. "coach-home-statement-portrait")
    # or bare variant_id (e.g. "statement-portrait"). The bare form lets a
    # cross-archetype hint like statement-portrait reuse one keeper signal
    # across every archetype's sibling group.
    raw_hint = meta.get("layout_variant_hint")
    if isinstance(raw_hint, str):
        hint = raw_hint.strip().lower()
        if hint:
            for c in candidates:
                if c.get("_key") == hint:
                    return c["_key"]
            for c in candidates:
                if (c.get("variant_id") or "").strip().lower() == hint:
                    return c["_key"]

    # Score remaining candidates.
    scored = [(c, _score_layout_against_meta(c, meta)) for c in candidates]
    top_score = max(s for _, s in scored)
    top = [c for c, s in scored if s == top_score]

    if len(top) == 1:
        return top[0]["_key"]

    # When scoring is all-zero (no keeper signal), exclude opt-in
    # single-page-feel variants from the tie-break pool. A statement-
    # portrait sibling deliberately drops the archetype's default
    # widget composition (offerings, testimonials, CTA, etc.) and
    # renders only a hero + portrait. A no-signal build random-walking
    # into that layout strips the home page of the content the
    # archetype was designed to surface. The variant remains
    # selectable via explicit `layout_variant_hint=statement-portrait`
    # or via positive mood-keyword scoring (minimal, spare, quiet).
    if top_score == 0:
        spreadable = [
            c for c in top
            if "single-page-feel" not in (c.get("tags") or [])
        ]
        if spreadable:
            top = spreadable
            if len(top) == 1:
                return top[0]["_key"]

    # Tie-break deterministically. Sort by _key first so the order is
    # stable regardless of dict iteration order, then index by seed.
    top_sorted = sorted(top, key=lambda c: c["_key"])
    idx = _stable_index(seed, f"layout-variant:{archetype}:{page}", len(top_sorted))
    return top_sorted[idx]["_key"]


def _validate_registry_id(value: str, valid_ids: List[str]) -> Optional[str]:
    """Return value if it's in the registry list; None otherwise.

    Caller decides whether to log an `_translation_warnings` entry on
    miss; we deliberately keep this pure so it's safe to call from
    both extraction (where we want to warn) and downstream readers
    (where we just want best-effort fallback).
    """
    return value if value in valid_ids else None


def _extract_palette_from_v1(v1_spec: dict) -> Optional[str]:
    """Extract a palette id, honoring (in order):
      1. `_harbor_meta.palette_hint` (validated against palettes.json),
      2. v1 `design.primary_color` hex mapping,
      3. None (scaffold falls back to rotation through archetype defaults).
    """
    # (1) Explicit hint from Haven extraction wins, if it names a real palette.
    hint = _harbor_meta_hint(v1_spec, "palette_hint")
    if hint:
        valid_ids = [p["id"] for p in load_palettes().get("palettes", [])]
        validated = _validate_registry_id(hint, valid_ids)
        if validated:
            return validated
        # Unknown hint: drop silently here; scaffold caller can add a
        # _translation_warnings entry. We don't have a write channel
        # to warnings from this pure helper.

    design = v1_spec.get("design") or {}
    primary = (design.get("primary_color") or "").lower()
    if not primary.startswith("#"):
        return None
    # Rough hex-family lookup, not an exact match since v1 hex values
    # are free-form. Just steer toward the palette that matches intent.
    map_table = [
        ("#6b1519", "warm-earth"), ("#9d2c4a", "soft-rose"),
        ("#064e3b", "forest-emerald"), ("#4c1d95", "deep-plum"),
        ("#0187a0", "ocean-blue"), ("#1e293b", "slate-gold"),
        ("#1e3a8a", "navy-clean"), ("#a0522d", "terracotta-sage"),
    ]
    for hex_val, pal in map_table:
        if hex_val == primary:
            return pal
    # Family-based fuzzy mapping by first two characters.
    prefix = primary[:3]
    family = {
        "#0": "navy-clean",   # blues / darks
        "#1": "slate-gold",   # navy / slate
        "#6": "warm-earth",   # browns / wines
        "#9": "soft-rose",    # rose / mauve
        "#a": "terracotta-sage",
        "#c": "warm-earth",
        "#4": "deep-plum",
    }
    return family.get(prefix)


def _extract_font_from_v1(v1_spec: dict) -> Optional[str]:
    """Extract a font_pair id, honoring (in order):
      1. `_harbor_meta.font_pair_hint` (validated against font-pairs.json),
      2. v1 `design.style` free-form mapping,
      3. None (scaffold falls back to rotation through archetype defaults).
    """
    hint = _harbor_meta_hint(v1_spec, "font_pair_hint")
    if hint:
        valid_ids = [f["id"] for f in load_font_pairs().get("font_pairs", [])]
        validated = _validate_registry_id(hint, valid_ids)
        if validated:
            return validated

    style = ((v1_spec.get("design") or {}).get("style") or "").lower()
    if not style:
        return None
    if any(k in style for k in ("modern", "tech", "saas", "clean")):
        return "inter-inter"
    if any(k in style for k in ("editorial", "serif", "magazine", "authority")):
        return "lora-inter"
    if any(k in style for k in ("luxury", "premium", "elegant", "sophisticated")):
        return "playfair-source-sans"
    if any(k in style for k in ("bold", "sharp", "agency", "fitness")):
        return "archivo-dm-sans"
    if any(k in style for k in ("warm", "approachable", "friendly")):
        return "rubik-nunito"
    if any(k in style for k in ("classical", "literary", "refined")):
        return "cormorant-lato"
    return None


# v1 field `type` → v2 `sql_type` mapping.
#
# v1 AppSpec fields declare a short type string like `"string"`, `"text"`,
# `"email"`, `"timestamp"`. v2 compose/build.py:_sql_type_for looks for
# either `sql_type` (VARCHAR(255)) or `field_type` (registry key like
# "email", "phone_us"). Without this map, a v1-translated customer
# data_model would emit every column as TEXT (the silent fallback), losing
# type safety + admin-form validation.
_V1_FIELD_TYPE_TO_SQL = {
    "string": "VARCHAR(255)",
    "text": "TEXT",
    "integer": "INTEGER",
    "int": "INTEGER",
    "number": "NUMERIC",
    "numeric": "NUMERIC",
    "float": "NUMERIC",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "email": "VARCHAR(255)",    # paired with field_type="email" below
    "phone": "VARCHAR(50)",     # paired with field_type="phone_us" below
    "url": "TEXT",              # paired with field_type="url" below
    "timestamp": "TIMESTAMPTZ",
    "datetime": "TIMESTAMPTZ",
    "date": "DATE",
    "time": "TIME",
    "password": "TEXT",
}

# v1 field types that deserve a field_type annotation too (so admin forms
# render with proper validation + sanitize chain from the v2 field-types
# registry, not just raw VARCHAR inputs).
_V1_FIELD_TYPE_TO_REGISTRY_KEY = {
    "email": "email",
    "phone": "phone_us",
    "url": "url",
}


def _normalize_v1_field(field: dict) -> dict:
    """Normalize a v1 field dict into v2 shape (sql_type + optional
    field_type). Preserves name, required, default, and any explicit
    v2-shape keys already present."""
    out = dict(field)
    v1_type = (field.get("type") or "").strip().lower()
    if v1_type and not out.get("sql_type"):
        out["sql_type"] = _V1_FIELD_TYPE_TO_SQL.get(v1_type, "TEXT")
    if v1_type and not out.get("field_type"):
        ft = _V1_FIELD_TYPE_TO_REGISTRY_KEY.get(v1_type)
        if ft:
            out["field_type"] = ft
    # Boolean columns must carry a default; v1 callers often omit it.
    if out.get("sql_type") == "BOOLEAN" and "default" not in out:
        out["default"] = "FALSE"
    return out


def _as_role_list(value):
    """A page's allowed_roles as the build engine expects it.

    A single role written as a string ("admin") means that one role:
    list("admin") would split it into letters, and a page limited to the
    roles "a", "d", "m", "i", "n" can be opened by nobody. An absent or
    empty value is no restriction beyond the login. A list passes through
    as it is, and so does any other shape, which the engine's gate then
    refuses by name (compose.build._spec_safety_errors) instead of this
    translator guessing what was meant.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, tuple):
        return list(value)
    return value


def _normalize_v1_data_model(model: dict) -> dict:
    """Normalize a v1 data_model dict into v2 shape.

    Preserves the customer's `name` + `belongs_to` + `fields` (normalized).
    Does NOT add `plural`: compose/build.py's `_slug_for` falls back to
    `_plural(name)` when plural is missing, so we can let the compose
    helper own that (keeps one source of truth).
    """
    out = dict(model)
    if isinstance(out.get("fields"), list):
        out["fields"] = [_normalize_v1_field(f) for f in out["fields"]]
    return out


# --- Shape of a v1 spec ------------------------------------------------------
# A v1 spec is JSON written by a model, like everything else that reaches
# this module. The translator below reads these keys as text or as lists
# (`.strip()`, `.lower()`, iteration). `_v1_shape_errors` names every value
# of another JSON type up front, and translate_v1_spec refuses the spec with
# the validator's wording ("Spec validation failed: ...") rather than failing
# with an AttributeError somewhere in the middle. A JSON null always means
# "absent". What the translator already handles itself is NOT refused here:
# an entry of one of the lists that is not an object, and a page `path` that
# is not a string, are each dropped with a warning; a `_seed_settings` or
# `_harbor_meta` that is not an object is ignored.
_V1_TOP_TEXT_KEYS = ("app_name", "description", "site_name", "admin_email", "tier")
_V1_TOP_LIST_KEYS = ("data_models", "pages", "user_roles", "actions")
_V1_MODEL_TEXT_KEYS = ("name", "plural", "slug", "description")
_V1_FIELD_TEXT_KEYS = ("name", "type", "sql_type", "field_type", "label")
_V1_PAGE_TEXT_KEYS = (
    "title", "purpose", "description", "form_target", "submit_text", "success_message",
)
_V1_ROLE_TEXT_KEYS = ("role", "description")
_V1_ACTION_TEXT_KEYS = ("name", "description")
# _seed_settings: every value is text, except these.
_V1_SEED_LIST_KEYS = ("credentials_lines", "home_body_blocks")
_V1_SEED_OBJECT_KEYS = ("social",)
# _harbor_meta: the hints read as text, the keyword lists, and the text
# keys of each service and testimonial the conversation produced.
_V1_META_TEXT_KEYS = (
    "archetype_hint", "template_family_hint", "layout_variant_hint", "complexity_hint",
    "density_hint", "hero_style_hint", "color_mood", "copy_source_url",
)
_V1_META_STRING_LIST_KEYS = ("mood_keywords", "avoid_keywords")
_V1_SERVICE_TEXT_KEYS = (
    "title", "name", "label", "description", "price_display", "cta_text", "cta_href", "image_url",
)
_V1_TESTIMONIAL_TEXT_KEYS = ("quote", "author", "author_name", "service", "role")
_MAX_V1_SHAPE_ERRORS = 20


def _v1_shape_errors(v1_spec: object) -> list:
    """Every value of a v1 spec that has the wrong JSON type for the way
    the translator reads it, as messages (at most `_MAX_V1_SHAPE_ERRORS`)."""
    if not isinstance(v1_spec, dict):
        return [f"the spec must be a JSON object, not {type(v1_spec).__name__}"]
    # Size first, with the engine's own bounds: everything below, and the
    # translator after it, walks the whole spec.
    too_big = _json_size_errors([("spec", v1_spec)])
    if too_big:
        return too_big
    errors: list = []

    def text(owner: dict, keys, where: str) -> None:
        for key in keys:
            value = owner.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f"{where}{key} must be a string")

    text(v1_spec, _V1_TOP_TEXT_KEYS, "")
    for key in _V1_TOP_LIST_KEYS:
        if v1_spec.get(key) is not None and not isinstance(v1_spec.get(key), list):
            errors.append(f"{key} must be a list")

    def entries(key: str):
        value = v1_spec.get(key)
        return list(enumerate(value)) if isinstance(value, list) else []

    for index, model in entries("data_models"):
        if not isinstance(model, dict):
            continue
        where = f"data_models[{index}]."
        text(model, _V1_MODEL_TEXT_KEYS, where)
        fields = model.get("fields")
        if fields is not None and not isinstance(fields, list):
            errors.append(f"{where}fields must be a list")
            continue
        for field_index, field in enumerate(fields or []):
            if not isinstance(field, dict):
                errors.append(f"{where}fields[{field_index}] must be an object")
                continue
            text(field, _V1_FIELD_TEXT_KEYS, f"{where}fields[{field_index}].")
        seed_rows = model.get("seed_rows")
        if seed_rows is not None and not isinstance(seed_rows, list):
            errors.append(f"{where}seed_rows must be a list")
    for index, page in entries("pages"):
        if isinstance(page, dict):
            text(page, _V1_PAGE_TEXT_KEYS, f"pages[{index}].")
            listed = page.get("allowed_fields")
            if listed is not None and not (
                isinstance(listed, list) and all(isinstance(item, str) for item in listed)
            ):
                errors.append(f"pages[{index}].allowed_fields must be a list of strings")
    for index, role in entries("user_roles"):
        if isinstance(role, dict):
            text(role, _V1_ROLE_TEXT_KEYS, f"user_roles[{index}].")
    for index, action in entries("actions"):
        if isinstance(action, dict):
            text(action, _V1_ACTION_TEXT_KEYS, f"actions[{index}].")

    seed = v1_spec.get("_seed_settings")
    if isinstance(seed, dict):
        for key, value in seed.items():
            if value is None:
                continue
            if key in _V1_SEED_LIST_KEYS:
                if not isinstance(value, list):
                    errors.append(f"_seed_settings.{key} must be a list")
            elif key in _V1_SEED_OBJECT_KEYS:
                if not isinstance(value, dict):
                    errors.append(f"_seed_settings.{key} must be an object")
                else:
                    text(value, list(value), f"_seed_settings.{key}.")
            elif not isinstance(value, str):
                errors.append(f"_seed_settings.{str(key)[:60]} must be a string")

    meta = v1_spec.get("_harbor_meta")
    if isinstance(meta, dict):
        text(meta, _V1_META_TEXT_KEYS, "_harbor_meta.")
        for key in _V1_META_STRING_LIST_KEYS:
            value = meta.get(key)
            if value is not None and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                errors.append(f"_harbor_meta.{key} must be a list of strings")
        for key, text_keys in (
            ("services", _V1_SERVICE_TEXT_KEYS), ("testimonials", _V1_TESTIMONIAL_TEXT_KEYS),
        ):
            value = meta.get(key)
            if value is None:
                continue
            if not isinstance(value, list):
                errors.append(f"_harbor_meta.{key} must be a list")
                continue
            for index, entry in enumerate(value):
                if isinstance(entry, dict):
                    text(entry, text_keys, f"_harbor_meta.{key}[{index}].")
    return errors[:_MAX_V1_SHAPE_ERRORS]


def translate_v1_spec(v1_spec: dict, admin_email: Optional[str] = None, app_name: Optional[str] = None) -> dict:
    """Translate a v1 AppSpec (from the existing web/ intake) into a v2
    spec ready for `compose.build.build_app`.

    The v1 intake is richer in structural detail (user_roles, data_models,
    actions) but thinner on the content/archetype/palette/font picks that
    v2 needs. This translator:
      - classifies the archetype from description + model + action text
      - derives palette + font from v1 design hints (with sensible fallbacks)
      - maps description → site_description + hero_description
      - maps v1 app_name → v2 app_name
      - forwards admin_email, contact hints where present
      - delegates to scaffold_spec for all archetype-defaulted page + seed content

    Option A pivot (2026-04-22): v2 now PRESERVES customer-declared
    v1.data_models alongside archetype defaults. Fields are normalized
    from v1 shape (`type: "string"`) to v2 shape (`sql_type:
    "VARCHAR(255)"`). Downstream `compose.build.build_app` merges the
    spec's `data_models` into the archetype's default models, so customer
    Class / Booking / whatever now flows through to schema + admin CRUD.

    v1.pages and v1.user_roles are carried through as custom_pages and
    user_roles (below); v1.actions only feed the archetype classification.

    A v1 spec whose values have the wrong JSON type for the way they are
    read here is refused first (`_v1_shape_errors`), with ValueError
    "Spec validation failed: ...".
    """
    shape_errors = _v1_shape_errors(v1_spec)
    if shape_errors:
        raise ValueError("Spec validation failed: " + "; ".join(shape_errors))
    archetype = detect_archetype(v1_spec)
    name = app_name or v1_spec.get("app_name") or "unnamed-app"
    # Normalize spacing and case to kebab-case. This is a convenience, not
    # the safety check: a name with "/" or ".." passes through unchanged
    # and is refused later by compose.build's spec safety gate.
    app_name_safe = "-".join(
        str(name).strip().lower().replace("_", "-").split()
    ) or "unnamed-app"
    description = v1_spec.get("description") or ""

    # v1 specs often carry customer-declared display copy in
    # `_seed_settings` (hero_tagline, about_body, contact_email, social
    # URLs, site_name, etc.). This is the primary source of truth for
    # customer copy. Pass through to scaffold_spec's answers so the
    # resulting v2 spec uses the customer's words, not archetype
    # defaults or app_name-derived title case (without the
    # pass-through, a _seed_settings block with a custom site_name,
    # hero_tagline and about copy is dropped in favor
    # of an app_name-derived title ("Sample Practitioner V1 123456") and
    # the archetype default tagline).
    v1_seed = v1_spec.get("_seed_settings") or {}
    if not isinstance(v1_seed, dict):
        v1_seed = {}
    # Humanize app_name for site_name if not otherwise provided.
    # When the slug has "-and-" between two non-empty words, recover the
    # ampersand: harbor's slug pipeline replaces "&" with "-and-" when
    # producing kebab-case; rendering "Fern And Stone" loses the
    # keeper's original brand.
    def _humanize_app_name(slug: str) -> str:
        words = [w for w in slug.split("-") if w]
        if not words:
            return ""
        out: list = []
        for i, w in enumerate(words):
            if w.lower() == "and" and 0 < i < len(words) - 1:
                out.append("&")
            else:
                out.append(w.capitalize())
        return " ".join(out)

    site_name = (
        v1_seed.get("site_name")
        or v1_spec.get("site_name")
        or _humanize_app_name(app_name_safe)
    )
    # The v1 description is typically a PROJECT BRIEF written for the
    # builder, not customer-facing copy ("Website needs to showcase
    # credentials, drive phone-call leads, etc"). Strip the worst
    # offenders so the hero doesn't surface internal language.
    def _clean_hero_description(s: str) -> str:
        if not s:
            return ""
        # Drop the most common prompt-leak phrases.
        lower = s.lower()
        bad_markers = [
            "website needs to", "website will", "website that",
            "needs to showcase", "drive phone-call", "drive phone call",
            "lead-gen", "lead generation", "quote-request", "quote request",
        ]
        if any(m in lower for m in bad_markers):
            # Keep only the first sentence if it's under ~120 chars, else
            # return empty and let the archetype default take over.
            first = s.split(".")[0].strip()
            if first and len(first) < 120 and not any(m in first.lower() for m in bad_markers):
                return first + "."
            return ""
        return s
    hero_desc = _clean_hero_description(description)
    # Curate tagline too: the v1 app_name often humanizes to garbage
    # like "Timber Line Hvac" (weak title case, initialism not
    # uppercased). Keep tagline short + archetype-flavored instead.
    # Archetype-specific tagline with one italic-accent word wrapped in
    # {braces}. The hero widget renders that word in italic serif
    # primary-color (the CAS "emotional emphasis" pattern).
    _archetype_tagline = {
        "healer-wellness": "Rest, breath, and steady {healing}",
        "coach-consultant": "Clarity under {pressure}",
        "local-service": "Local, licensed, {reliable}",
        "therapist-counselor": "Compassionate, evidence-based {counseling}",
        "fitness-studio": "Move, breathe, {belong}",
        "salon-spa": "Intentional hair, {craft}-first",
        "restaurant-cafe": "A neighborhood {table}",
        "tradesperson-contractor": "Licensed, insured, {family-run}",
    }
    hero_tag = _archetype_tagline.get(archetype, site_name)
    # translation_warnings: a metadata channel that records what the
    # translator did with a customer spec.
    # Appended to throughout this function and surfaced on the v2 spec
    # as `_translation_warnings`. Each entry: {reason: str, detail: str}.
    warnings: list = []

    # Preserve customer-declared data_models. The list is normalized
    # v1 → v2 field-shape; compose/build.py (`build_app`) then merges
    # spec.data_models into the archetype defaults (a customer model replaces
    # a default of the same name) and `_slug_for` + `_default_fields`
    # fallbacks cover anything left unset.
    v1_models = v1_spec.get("data_models") or []
    normalized_models = []
    for m in v1_models:
        if not isinstance(m, dict):
            warnings.append({"reason": "invalid_data_model",
                             "detail": f"non-dict data_model dropped: {type(m).__name__}"})
            continue
        if not m.get("name"):
            warnings.append({"reason": "data_model_missing_name",
                             "detail": "data_model entry had no name"})
            continue
        normalized_models.append(_normalize_v1_data_model(m))

    # Preserve customer-declared pages that DON'T collide with
    # archetype defaults (home/about/contact/etc.). Archetype-collision
    # filtering happens in compose.build._custom_pages_filter(rs). We
    # pass everything through here and let the build-time filter + view
    # emitter decide what to emit. See build.py:emit_custom_page_views.
    v1_pages = v1_spec.get("pages") or []
    custom_pages = []
    # The paths the archetype serves a public page at (its default pages).
    archetype_public_keys = {
        _path_key("/" if page_id == "home" else f"/{page_id}")
        for page_id in (get_archetype(archetype).get("default_pages") or [])
    }
    for p in v1_pages:
        if not isinstance(p, dict):
            warnings.append({"reason": "invalid_page",
                             "detail": f"non-dict page dropped: {type(p).__name__}"})
            continue
        raw_path = p.get("path")
        path = raw_path.strip() if isinstance(raw_path, str) else ""
        path_problem = None
        if not path or not path.startswith("/"):
            path_problem = "path missing or not absolute"
        # Same rule compose.build enforces at emit time: a page path is
        # written into route source and a view file name, so only plain
        # segments are kept. (Parametric paths such as /events/:slug pass
        # through here and are dropped by the build's own filter.)
        elif not _is_safe_route_path(path) and not _is_parametric_path(path):
            path_problem = "path is not a plain route path"
        if path_problem:
            warnings.append({"reason": "invalid_page_path",
                             "detail": f"page dropped; {path_problem}: {path[:80]!r}"})
            if p.get("auth_required"):
                # The same reason the build engine records when IT drops a
                # page that was meant to sit behind a login
                # (compose.build._custom_page_warnings).
                warnings.append({
                    "reason": "auth_required_page_not_emitted",
                    "detail": (
                        f"custom page {path[:80]!r} is marked auth_required but was not "
                        f"emitted ({path_problem}); no protected page exists at that path"
                    ),
                })
            continue
        # Public submission form hints pass through. form_target
        # names a data_model the page's form should POST into; allowed_fields
        # optionally restricts which model fields are rendered (default: all
        # non-system fields). submit_text + success_message customize the CTA
        # and post-submit flash.
        page_roles = _as_role_list(p.get("allowed_roles"))
        page_auth_required = bool(p.get("auth_required"))
        if not page_auth_required and _roles_imply_login({"allowed_roles": page_roles}):
            # A list of roles is a restriction (compose.build._roles_imply_login).
            # A v1 spec often wrote "the admin manages this, everyone can
            # view it" as auth_required false with allowed_roles ["admin"]
            # on a page the archetype already serves publicly (/blog). That
            # page is left out here: the archetype's own page answers at
            # that path, which is the "everyone can view" half, and a
            # protected page at a path another page answers without a login
            # is a spec the engine refuses. Any other page that lists roles
            # asks for a login and one of those roles. Both are said in a
            # warning.
            if _path_key(path) in archetype_public_keys:
                warnings.append({
                    "reason": "allowed_roles_page_dropped",
                    "detail": (
                        f"page {path[:80]!r} lists allowed_roles but is not marked auth_required, and "
                        "the archetype already serves a public page at that path; the page was left out "
                        "and the archetype's page answers there"
                    ),
                })
                continue
            page_auth_required = True
            warnings.append({
                "reason": "allowed_roles_imply_login",
                "detail": (
                    f"page {path[:80]!r} lists allowed_roles but is not marked auth_required; "
                    "a list of roles is a restriction, so the page requires a login and one of those roles"
                ),
            })
        custom_pages.append({
            "path": path,
            "title": p.get("title") or "",
            "purpose": p.get("purpose") or "",
            "auth_required": page_auth_required,
            "allowed_roles": page_roles,
            "form_target": (p.get("form_target") or "").strip() or None,
            "allowed_fields": list(p.get("allowed_fields") or []) or None,
            "submit_text": (p.get("submit_text") or "").strip() or None,
            "success_message": (p.get("success_message") or "").strip() or None,
        })

    # Preserve customer-declared user_roles. v2's
    # users.role column is free-form VARCHAR(50) and requireRole
    # middleware already works with any string, so no schema change.
    # We pass the role list through as metadata; downstream
    # (build.py:emit_seed_sql) emits a documentation comment block
    # listing the roles so reviewers + admins see them.
    v1_roles = v1_spec.get("user_roles") or []
    user_roles = []
    for r in v1_roles:
        if not isinstance(r, dict):
            warnings.append({"reason": "invalid_user_role",
                             "detail": f"non-dict user_role dropped: {type(r).__name__}"})
            continue
        role_name = (r.get("role") or "").strip()
        if not role_name:
            warnings.append({"reason": "user_role_missing_name",
                             "detail": "user_role entry had no role field"})
            continue
        user_roles.append({
            "role": role_name,
            "description": r.get("description") or "",
        })

    # Customer-declared _seed_settings take precedence over archetype
    # defaults + translator-derived strings. Only the keys present in
    # _seed_settings override; missing keys fall through to the
    # translator's best guess (description cleanup, archetype tagline,
    # etc.).
    def _first(*values):
        """Return the first non-empty value. Empty string counts as empty."""
        for v in values:
            if v not in (None, ""):
                return v
        return None

    # Harbor pipeline extras: services + testimonials extracted by Opus from
    # the keeper conversation are stored in v1_spec._harbor_meta by
    # services/harbor_to_compose.ts. Without this pass-through they would
    # be silently dropped and scaffold_spec's archetype defaults (executive
    # coaching for coach-consultant, etc.) would render instead, leaving a
    # tutor's or an instructor's build with mismatched offerings copy. Map the harbor data shape to the compose default
    # shape so scaffold_spec's `answers.get("services") or _default_...`
    # lookup honors the customer's words.
    harbor_meta = v1_spec.get("_harbor_meta") or {}
    if not isinstance(harbor_meta, dict):
        harbor_meta = {}
    harbor_services = _normalize_harbor_services(harbor_meta.get("services"))
    # Systemic fallback: when
    # _harbor_meta.services is empty (Opus's section vocabulary varied
    # too far from harbor_to_compose's alias map), reconstruct services
    # from any offering-style data_model's seed_rows. Opus's prompt
    # explicitly asks for seed_rows when keeper mentioned prices, so
    # this catches the case where prices ARE in seed_rows but never
    # made it into a sections-of-type-offerings block on any page.
    if not harbor_services:
        harbor_services = _services_from_offering_seed_rows(v1_spec)
    harbor_testimonials = _normalize_harbor_testimonials(harbor_meta.get("testimonials"))

    answers = {
        "app_name": app_name_safe,
        "archetype": archetype,
        "site_name": site_name,
        # site_description flows into <meta name="description">, OG tags,
        # the layout fallback, and the dashboard preview card. We always
        # truncate to a clean meta-snippet length (~155 chars) so the long
        # customer brief doesn't blow out search engine snippets or get
        # echoed verbatim into the hero. v1.description / hero_desc
        # (cleaned) is the next-best signal when no explicit setting.
        "site_description": _truncate_meta_description(
            _first(
                v1_seed.get("site_description"),
                hero_desc,
                description if description else None,
                site_name,
            )
        ),
        "hero_tagline": _first(v1_seed.get("hero_tagline"), hero_tag),
        # hero_description is the optional supporting paragraph below the
        # H1. We do NOT auto-fill it from v1.description any more; that
        # produced builds where site_description and hero_description were
        # the same long string, and the hero rendered the SEO meta as a
        # wall of body copy. Only honor an explicit _seed_settings value
        # so the keeper / admin can opt in to a hero paragraph.
        "hero_description": _first(v1_seed.get("hero_description"), ""),
        "services": harbor_services or None,
        "testimonials": harbor_testimonials or None,
        "hero_image_url": v1_seed.get("hero_image_url") or None,
        "about_title": v1_seed.get("about_title") or None,
        "about_body": v1_seed.get("about_body") or None,
        "about_photo_url": v1_seed.get("about_photo_url") or None,
        "nav_logo_url": v1_seed.get("nav_logo_url") or None,
        "services_image_url": v1_seed.get("services_image_url") or None,
        "services_intro": v1_seed.get("services_intro") or None,
        # Statement-portrait home layout fields (consumed by
        # hero-two-col-statement widget; ignored by archetype-default hero
        # widgets).
        "credentials_lines": v1_seed.get("credentials_lines") or None,
        "home_body_blocks": v1_seed.get("home_body_blocks") or None,
        "statement_cta_text": v1_seed.get("statement_cta_text") or None,
        "contact_email": _first(
            v1_seed.get("contact_email"),
            admin_email,
            v1_spec.get("admin_email"),
        ),
        "contact_phone": v1_seed.get("contact_phone") or None,
        "city_state": v1_seed.get("contact_address") or v1_seed.get("city_state") or None,
        "business_name": v1_seed.get("business_name") or None,
        "social": {
            k: v for k, v in (v1_seed.get("social") or {
                "instagram_url": v1_seed.get("instagram_url", ""),
                "facebook_url": v1_seed.get("facebook_url", ""),
                "twitter_url": v1_seed.get("twitter_url", ""),
                "linkedin_url": v1_seed.get("linkedin_url", ""),
                "youtube_url": v1_seed.get("youtube_url", ""),
                "tiktok_url": v1_seed.get("tiktok_url", ""),
                "venmo_handle": v1_seed.get("venmo_handle", ""),
            }).items() if v
        } or None,
        "admin_email": admin_email or v1_spec.get("admin_email") or "admin@example.com",
        "palette": _extract_palette_from_v1(v1_spec),
        "font_pair": _extract_font_from_v1(v1_spec),
        "data_models": normalized_models or None,  # None → archetype defaults
        "custom_pages": custom_pages or None,      # None → no custom pages
        "user_roles": user_roles or None,           # passed through
        "_translation_warnings": warnings or None,  # metadata channel
        # Pass _harbor_meta straight through so
        # scaffold_spec can stamp it onto the output spec for the build
        # stage's pick_layout_variant call.
        "_harbor_meta": v1_spec.get("_harbor_meta") if isinstance(v1_spec.get("_harbor_meta"), dict) else None,
    }
    # Drop None values so scaffold_spec uses archetype defaults.
    answers = {k: v for k, v in answers.items() if v is not None}
    return scaffold_spec(answers)


def run_intake(answers: dict, write_fixture: bool = True, run_build: bool = False, output_dir: Optional[Path] = None) -> dict:
    spec = scaffold_spec(answers)
    # Validate before writing
    errors = validate_spec(BuildSpec.from_dict(spec))
    if errors:
        raise ValueError("scaffolded spec failed validation: " + "; ".join(errors))
    if write_fixture:
        # Avoid double-prefix when answers already name things with
        # "sample-" (e.g. sample-coach → sample-coach-spec.json, not
        # sample-sample-coach-spec.json).
        stem = spec["app_name"]
        if not stem.startswith("sample-"):
            stem = f"sample-{stem}"
        fixture_path = FIXTURES_DIR / f"{stem}-spec.json"
        fixture_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote fixture → {fixture_path}")
    if run_build:
        out = build_app(spec, output_dir=output_dir)
        print(f"Composed → {out}")
    return spec


def main():
    args = sys.argv[1:]
    build = False
    from_v1 = False
    if "--build" in args:
        build = True
        args = [a for a in args if a != "--build"]
    if "--from-v1" in args:
        from_v1 = True
        args = [a for a in args if a != "--from-v1"]

    if not args or args[0] == "--stdin":
        data = json.load(sys.stdin)
    else:
        with open(args[0]) as f:
            data = json.load(f)

    if from_v1:
        # v1 AppSpec → v2 spec translator. Pass through admin_email if
        # the caller provided one (e.g. the worker enriches it from the
        # builds table before handing the AppSpec here).
        spec = translate_v1_spec(
            data,
            admin_email=data.get("admin_email"),
            app_name=data.get("app_name"),
        )
    else:
        spec = scaffold_spec(data)

    # Validate BEFORE the app_name is used as a file name: it comes from the
    # input file, and an unchecked "../x" would write outside fixtures/.
    errors = validate_spec(BuildSpec.from_dict(spec))
    if errors:
        raise SystemExit("scaffolded spec failed validation: " + "; ".join(errors))

    # Write fixture + optionally build.
    stem = spec["app_name"]
    if not stem.startswith("sample-"):
        stem = f"sample-{stem}"
    fixture_path = FIXTURES_DIR / f"{stem}-spec.json"
    fixture_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote fixture → {fixture_path}  (archetype: {spec['archetype']}, palette: {spec['palette']}, fonts: {spec['font_pair']})")
    if build:
        out = build_app(spec)
        print(f"Composed → {out}")


if __name__ == "__main__":
    main()
