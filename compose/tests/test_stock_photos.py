"""Tests for curated stock-photo selection + the compose copy step.

Guards the 2026-06-07 fix that replaced picsum.photos (random Unsplash by ID;
id/20 shipped corporate logos onto a coach's hero) with the self-hosted
cli/stock_photos/ library copied into the build at /images/stock/.
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from compose import stock_photos as sp


def test_selector_only_returns_existing_files():
    """Every pick across every archetype must resolve to a committed file
    (the manifest lists ~2x the files; a missing one would be a broken img)."""
    archetypes = list(sp._ARCHETYPE_CATEGORIES.keys()) + ["unknown-archetype"]
    for arch in archetypes:
        for name in ("alpha", "bravo", "charlie", "delta", "echo"):
            for use_case in ("hero", "about"):
                res = sp.select_stock_photo(arch, use_case, name)
                if res is not None:
                    cat, fn = res
                    assert sp.stock_photo_source_path(fn) is not None, (
                        f"{arch}/{name}/{use_case} -> {fn} has no source file"
                    )


def test_selector_is_deterministic():
    a = sp.select_stock_photo("healer-wellness", "hero", "Maya Reiki")
    b = sp.select_stock_photo("healer-wellness", "hero", "Maya Reiki")
    assert a == b


def test_selector_spreads_across_app_names():
    picks = {sp.select_stock_photo("healer-wellness", "hero", n)
             for n in ("a", "b", "c", "d", "e", "f", "g")}
    assert len([p for p in picks if p]) >= 2


def test_wedge_archetypes_lead_with_wellness_category():
    """Healer / coach / therapist should draw hero imagery from a wellness-
    leaning category, never a corporate/logo source. The chosen category for
    the wedge is one of the archetype's on-brand preferred categories."""
    for arch in ("healer-wellness", "coach-consultant", "therapist-counselor"):
        res = sp.select_stock_photo(arch, "hero", "Sample Somatic Coaching")
        assert res is not None
        cat, fn = res
        assert cat in sp._ARCHETYPE_CATEGORIES[arch]
        # First preferred category for all three is "wellness".
        assert sp._ARCHETYPE_CATEGORIES[arch][0] == "wellness"


def test_no_picsum_in_default_urls():
    for arch in sp._ARCHETYPE_CATEGORIES:
        h = sp.default_hero_image_url(arch, "X Co")
        a = sp.default_about_photo_url(arch, "X Co")
        assert "picsum" not in h and "picsum" not in a
        assert h == "" or h.startswith("/images/stock/")
        assert a == "" or a.startswith("/images/stock/")


def test_empty_manifest_yields_no_image(monkeypatch):
    """Degenerate: a missing/empty manifest must yield '' (text-forward hero),
    never a crash and never picsum."""
    monkeypatch.setattr(sp, "_manifest_cache", {})
    # _load_manifest returns the cache when set; force it empty.
    monkeypatch.setattr(sp, "_load_manifest", lambda: {})
    assert sp.default_hero_image_url("healer-wellness", "X") == ""
    assert sp.select_stock_photo("healer-wellness", "hero", "X") is None


_PERSONAL_PHOTOS = {
    "practitioner-portrait-01.jpg", "facilitator-portrait-01.jpg",
    "event-gathering-01.jpg", "event-venue-01.jpg", "event-venue-02.jpg",
    "dome-venue-01.jpg", "dome-venue-02.jpg",
}


def test_personal_photos_match_the_manifest():
    """Every name in _PERSONAL_PHOTOS is a real wellness manifest entry, so the
    guard below cannot silently drift away from the files it protects."""
    wellness = {p["filename"] for p in sp._load_manifest().get("wellness", [])}
    assert _PERSONAL_PHOTOS <= wellness, _PERSONAL_PHOTOS - wellness


def test_personal_photos_are_flagged():
    """The practitioner/facilitator portraits and the dome/gathering/venue
    photos (seeded into the wellness category) must be detected as personal
    (content-integrity)."""
    for fn in _PERSONAL_PHOTOS:
        assert sp._is_personal_photo({"filename": fn, "alt_text": "", "tags": []}), fn
    # The real manifest entries (filename + alt + tags) are flagged too.
    for photo in sp._load_manifest().get("wellness", []):
        if photo["filename"] in _PERSONAL_PHOTOS:
            assert sp._is_personal_photo(photo), photo["filename"]
    # Generic anonymous stock is NOT flagged.
    for fn in ("aromatherapy-01.jpg", "ceremony-01.jpg", "sound-healing-01.jpg",
               "meditation-01.jpg", "crystals-02.jpg", "massage-02.jpg",
               "professional-headshot-01.jpg", "cafe-interior-01.jpg"):
        assert not sp._is_personal_photo({"filename": fn, "alt_text": fn, "tags": []}), fn


def test_personal_photos_never_selected_anywhere():
    """A build must never select a real person's face or a recognizable
    third-party venue/community for a customer's site: stock photos are
    chosen by code, and nobody looks at the choice when the site is built.
    Exhaustive guard across every archetype + use_case + a spread of app_names."""
    selected = set()
    for arch in list(sp._ARCHETYPE_CATEGORIES) + ["unknown"]:
        for name in ("a", "b", "c", "d", "e", "f", "g", "h", "willow", "rooted", "maya", "fern"):
            for uc in ("hero", "about", "team", "gallery"):
                r = sp.select_stock_photo(arch, uc, name)
                if r:
                    selected.add(r[1])
    leaked = selected & _PERSONAL_PHOTOS
    assert not leaked, f"personal photos selected for customer use: {leaked}"


def test_coach_imagery_is_mood_aware():
    """coach-consultant is broad: a leadership/career coach (professional mood)
    must get business imagery, not wellness/spiritual photos (the automated
    build review, a loop that ran outside this repository and is not included
    in it, held a leadership coach's build for a singing-bowl hero,
    2026-06-07). A
    life/somatic coach (wellness mood) keeps wellness imagery."""
    prof = "Leadership and career coaching for mid-career professionals. Professional, executive, results."
    well = "Life and somatic coaching for burnout. Warm, grounded, earthy, embodied healing."
    assert sp._resolve_categories("coach-consultant", prof)[0] == "business"
    assert sp._resolve_categories("coach-consultant", well)[0] == "wellness"
    # No mood -> wellness-leaning default (correct for the core wedge).
    assert sp._resolve_categories("coach-consultant", "")[0] == "wellness"
    assert sp._resolve_categories("coach-consultant", None)[0] == "wellness"
    # Non-coach archetypes are unaffected by mood.
    assert sp._resolve_categories("healer-wellness", prof) == sp._ARCHETYPE_CATEGORIES["healer-wellness"]
    # End to end: a professional coach's hero comes from business, a somatic
    # coach's from wellness; both resolve to existing files.
    ph = sp.default_hero_image_url("coach-consultant", "Sample Executive Coaching", prof)
    wh = sp.default_hero_image_url("coach-consultant", "Sample Somatic Coaching", well)
    assert ph.startswith("/images/stock/") and sp.stock_photo_source_path(ph.rsplit("/", 1)[-1]) is not None
    assert wh.startswith("/images/stock/") and sp.stock_photo_source_path(wh.rsplit("/", 1)[-1]) is not None


def test_source_path_returns_none_for_unknown_file():
    assert sp.stock_photo_source_path("does-not-exist-xyz.jpg") is None
    assert sp.stock_photo_source_path("") is None


def test_compose_copies_referenced_stock_photo_into_build(tmp_path):
    """End-to-end: a healer build's hero (a curated /images/stock/ photo) must
    be copied into public/images/stock/<file> as real bytes, and the home view
    must reference it."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    v1 = {
        "app_name": "willow-sage-wellness",
        "description": "A Reiki and breathwork practice for stress and burnout.",
        "admin_email": "admin@example.com",
        "spec_version": "1.0",
        "data_models": [],
        "user_roles": ["admin"],
        "pages": [{"path": "/", "title": "Home"}],
        "_harbor_meta": {"archetype_hint": "healer-wellness"},
    }
    spec = translate_v1_spec(v1)
    out = build_app(spec, output_dir=tmp_path / "build")
    # A stock photo was copied in.
    stock_dir = out / "public" / "images" / "stock"
    copied = list(stock_dir.glob("*.jpg")) if stock_dir.exists() else []
    assert copied, "no curated stock photo was copied into public/images/stock/"
    # Each copied file is non-trivial bytes (a real image, not an empty stub).
    for f in copied:
        assert f.stat().st_size > 1000, f"{f.name} looks empty ({f.stat().st_size}b)"
    # The hero + about settings must reference the self-hosted /images/stock/
    # library, never picsum. (Secondary _seed_image card images are a separate,
    # still-deferred picsum slot, so we scope this to the hero/about settings.)
    seed_path = out / "db" / "seed.sql"
    assert seed_path.exists(), "db/seed.sql not emitted"
    seed = seed_path.read_text()
    setting_lines = [l for l in seed.splitlines()
                     if "site_settings" in l
                     and ("'hero_image_url'" in l or "'about_photo_url'" in l)]
    assert setting_lines, "no hero/about image settings seeded"
    for l in setting_lines:
        assert "picsum" not in l, f"picsum in prominent image setting: {l.strip()}"
        assert "/images/stock/" in l, f"hero/about not self-hosted: {l.strip()}"


# ---------------------------------------------------------------------------
# A spec cannot name one of the personal photos either
# ---------------------------------------------------------------------------

def _coach_spec() -> dict:
    import json
    path = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")


PERSONAL_REFS = [
    ("the hero setting", lambda spec: spec["settings"].__setitem__("hero_image_url", "/images/stock/practitioner-portrait-01.jpg")),
    ("the about photo setting", lambda spec: spec["settings"].__setitem__("about_photo_url", "/images/stock/dome-venue-02.jpg")),
    ("a seed row", lambda spec: spec["seed_content"].__setitem__(
        "testimonials", [{"author_name": "A", "quote": "q", "image_url": "/images/stock/event-gathering-01.jpg"}])),
    ("a page of the site", lambda spec: spec["pages"]["about"].__setitem__("photo_url", "/images/stock/facilitator-portrait-01.jpg")),
    ("free text", lambda spec: spec["settings"].__setitem__("about_body", 'See <img src="/images/stock/event-venue-01.jpg">.')),
]


@pytest.mark.parametrize("where, put", PERSONAL_REFS, ids=[p[0] for p in PERSONAL_REFS])
def test_a_spec_that_names_a_personal_photo_is_refused(where, put, no_network):
    """The selector never picks these (above). A spec that names one used to
    get it: the copy step copied whatever /images/stock/<file> the emitted
    text mentioned. Whoever picks the file, a real person's face or someone
    else's venue is not the customer's."""
    from compose.build import BuildSpec, validate_spec, resolve_spec
    spec = _coach_spec()
    put(spec)
    errors = validate_spec(BuildSpec.from_dict(spec))
    assert len(errors) == 1, errors
    assert "names the stock photo" in errors[0] and "shows a specific person or place" in errors[0]
    assert "never used for a customer's site" in errors[0]
    with pytest.raises(ValueError, match="names the stock photo"):
        resolve_spec(BuildSpec.from_dict(spec))


def test_a_generic_stock_photo_and_another_sites_address_are_not_refused(no_network):
    from compose.build import BuildSpec, validate_spec
    spec = _coach_spec()
    spec["settings"]["hero_image_url"] = "/images/stock/aromatherapy-01.jpg"
    # The same path on somebody else's host is that host's address.
    spec["settings"]["about_photo_url"] = "https://cdn.example.com/images/stock/event-gathering-01.jpg"
    assert validate_spec(BuildSpec.from_dict(spec)) == []


def test_the_build_does_not_copy_a_personal_photo_that_got_past_the_gate(tmp_path, monkeypatch, no_network):
    """The same rule in the copy step, for a ResolvedSpec that did not go
    through the gate: the file is not copied and the report says why."""
    from compose import build as B
    monkeypatch.setattr(B, "_personal_stock_photo_errors", lambda spec: [])
    spec = _coach_spec()
    spec["settings"]["hero_image_url"] = "/images/stock/practitioner-portrait-01.jpg"
    out = B.build_app(spec, output_dir=tmp_path / "site")
    assert not (out / "public" / "images" / "stock" / "practitioner-portrait-01.jpg").exists()
    manifest = (out / "compose-manifest.txt").read_text(encoding="utf-8")
    assert "warning: stock_photo_not_copied: practitioner-portrait-01.jpg: shows a specific person or place" in manifest


def test_the_name_check_reads_the_manifest_and_falls_back_to_the_file_name():
    for photo in (p for photos in sp._load_manifest().values() for p in photos):
        assert sp.is_personal_photo_name(photo["filename"]) == sp._is_personal_photo(photo), photo["filename"]
    assert sp.is_personal_photo_name("a-new-facilitator-photo.jpg")  # no manifest entry: by its name
    assert not sp.is_personal_photo_name("aromatherapy-01.jpg")
    assert not sp.is_personal_photo_name(None)
