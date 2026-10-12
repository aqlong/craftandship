"""Curated stock-photo selection for v2 builds.

Replaces the picsum.photos placeholder approach (random Unsplash photos by ID;
picsum.photos/id/20 is a logo-laden design desk that shipped onto a life
coach's hero AND about page on 2026-06-07) with the self-hosted, on-brand
library committed under cli/stock_photos/<category>/. The worker Docker image
carries these files (Dockerfile.worker COPY . .), so builds reference local
/images/stock/<filename> assets that compose copies into the build output: no
external dependency, no off-brand surprises, no third-party logos.

Selection is deterministic: same (archetype, use_case, app_name) maps to the
same photo across rebuilds; different app_names spread across the candidate
pool so two builds of the same archetype do not trivially share an image.
When no curated match exists the caller drops the image (text-forward hero),
which is always better than an off-brand placeholder.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Photos depicting a SPECIFIC identifiable person, or a specific real
# property / community, that were seeded into the (generic) wellness category:
# the practitioner / facilitator portraits and the dome, gathering and venue
# photos from one particular event series. These are NEVER used for a
# customer's site: the selector does not choose them, a spec that names one is
# refused (`compose.build._personal_stock_photo_errors`) and the build does not
# copy one in (`compose.build.compose_files`). Publishing a real person's
# face, or a recognizable third-party venue/community, as the customer's own
# is a content-integrity violation. The automated build review (a review loop that ran outside this
# repository and is not included in it) caught the gathering photo, and would
# have caught a portrait, on an unrelated preview. Generic
# anonymous stock (a model meditating, hands holding a singing bowl) stays
# fully eligible. Matched against filename + alt + tags so future
# similarly-named personal seeds are excluded too.
_PERSONAL_PHOTO_PATTERN = re.compile(
    r"practitioner[\s_-]?portrait|facilitator|gathering|"
    r"dome[\s_-]?venue|event[\s_-]?venue",
    re.IGNORECASE,
)


def _is_personal_photo(photo: dict) -> bool:
    """True when a manifest entry depicts a specific identifiable person or a
    specific real property/community (see _PERSONAL_PHOTO_PATTERN)."""
    if not isinstance(photo, dict):
        return False
    tags = photo.get("tags")
    blob = " ".join([
        str(photo.get("filename", "")),
        str(photo.get("alt_text", "")),
        " ".join(tags) if isinstance(tags, list) else "",
    ])
    return bool(_PERSONAL_PHOTO_PATTERN.search(blob))


def is_personal_photo_name(filename: str) -> bool:
    """True when the library file `filename` is one of the personal photos
    (see _PERSONAL_PHOTO_PATTERN): by its manifest entry when it has one
    (file name, alt text and tags), by its file name alone when it has not."""
    if not isinstance(filename, str):
        return False
    for photos in _load_manifest().values():
        for photo in (photos or []):
            if isinstance(photo, dict) and photo.get("filename") == filename:
                return _is_personal_photo(photo)
    return _is_personal_photo({"filename": filename})

_STOCK_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

_COMPOSE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _COMPOSE_DIR.parent
_STOCK_DIR = _REPO_ROOT / "cli" / "stock_photos"
_MANIFEST_PATH = _STOCK_DIR / "manifest.json"

# Archetype -> ordered preferred manifest categories. Healer / coach /
# therapist (the CAS wedge) lead with "wellness" so a somatic coach never gets
# a corporate-desk photo. The wellness category carries the most photos (18).
_ARCHETYPE_CATEGORIES: Dict[str, List[str]] = {
    "healer-wellness":         ["wellness", "nature", "beauty"],
    "therapist-counselor":     ["wellness", "people", "medical"],
    "coach-consultant":        ["wellness", "business", "people"],
    "salon-spa":               ["beauty", "wellness"],
    "fitness-studio":          ["fitness", "sports", "wellness"],
    "restaurant-cafe":         ["food", "hospitality"],
    "tradesperson-contractor": ["construction", "automotive", "business"],
    "local-service":           ["business", "construction", "hospitality"],
    "event-producer":          ["arts", "hospitality", "people"],
}
_DEFAULT_CATEGORIES = ["wellness", "business", "nature"]

# "coach-consultant" is a BROAD archetype: it covers wellness/life/somatic
# coaches (who want calm wellness imagery) AND leadership/executive/career
# coaches (who want professional/corporate imagery). Leading with "wellness"
# for everyone shipped singing-bowl + meditation photos onto a male leadership
# coach's site (the automated build review, not part of this repository, held
# that build, 2026-06-07). So for coaches
# we pick the category order from the keeper's own mood/description: a
# professional signal flips to business-first; otherwise the wellness-leaning
# default holds (correct for life/somatic coaches like the validated builds).
_PROFESSIONAL_SIGNAL = re.compile(
    r"\b(professional|corporate|leadership|executive|business|career|consult\w*|"
    r"strateg\w*|b2b|management|performance|results|growth|advisory|c-?suite)\b",
    re.IGNORECASE,
)
_WELLNESS_SIGNAL = re.compile(
    r"\b(wellness|holistic|spiritual|somatic|reiki|healing|sacred|nature|gentle|"
    r"nurtur\w*|embodied|breathwork|meditat\w*|mindful\w*|soul|intuitive|energy|"
    r"yoga|massage|bodywork|nervous system|burnout)\b",
    re.IGNORECASE,
)


def _resolve_categories(archetype: str, mood_text: Optional[str]) -> List[str]:
    """Preferred categories for the archetype, mood-aware for coach-consultant."""
    base = _ARCHETYPE_CATEGORIES.get(archetype) or _DEFAULT_CATEGORIES
    if archetype == "coach-consultant" and mood_text:
        prof = len(_PROFESSIONAL_SIGNAL.findall(mood_text))
        well = len(_WELLNESS_SIGNAL.findall(mood_text))
        if prof > well:
            return ["business", "people", "wellness"]
    return base


_manifest_cache: Optional[Dict[str, List[dict]]] = None


def _load_manifest() -> Dict[str, List[dict]]:
    """Load + cache the manifest. Returns {} on any failure (caller then drops
    the image rather than crashing the build)."""
    global _manifest_cache
    if _manifest_cache is None:
        try:
            with open(_MANIFEST_PATH, encoding="utf-8") as f:
                data = json.load(f)
            _manifest_cache = data if isinstance(data, dict) else {}
        except Exception:
            _manifest_cache = {}
    return _manifest_cache


def _stable_index(seed: str, salt: str, modulo: int) -> int:
    """Deterministic, well-distributed index in [0, modulo)."""
    if modulo <= 0:
        return 0
    payload = f"{seed or 'default'}|{salt or ''}".encode("utf-8")
    digest = hashlib.md5(payload, usedforsecurity=False).digest()
    return int.from_bytes(digest[:8], "little") % modulo


def select_stock_photo(
    archetype: str, use_case: str, app_name: str, orientation: Optional[str] = None,
    exclude: Optional[set] = None, mood_text: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """Pick (category, filename) for the archetype + use_case, deterministic by
    app_name.

    Walks the archetype's preferred categories IN ORDER and returns from the
    first category that has any usable photo, so the most on-brand category
    (e.g. "wellness" for a coach) wins over later fallbacks. Within a category:
    prefer a use_case match, then an orientation match, then deterministic
    hash-pick for spread across app_names.

    Only photos whose source FILE actually exists are eligible: the manifest
    lists ~2x the committed files, and referencing a missing /images/stock/
    asset would ship a broken <img>. Returns None when no curated photo is
    available (caller drops the image; never picsum)."""
    manifest = _load_manifest()
    if not manifest:
        return None
    cats = _resolve_categories(archetype, mood_text)
    _exclude = exclude or set()
    for cat in cats:
        present = [
            p for p in (manifest.get(cat) or [])
            if isinstance(p, dict) and p.get("filename")
            and p["filename"] not in _exclude  # so about never repeats the hero photo
            and not _is_personal_photo(p)  # content-integrity: no real-person/specific-venue photos
            and stock_photo_source_path(p["filename"]) is not None
        ]
        if not present:
            continue
        # Prefer use_case match, relax to the category's present photos.
        by_use = [p for p in present if use_case in (p.get("use_case") or [])]
        chosen = by_use or present
        # Prefer orientation match, relax if none qualify.
        if orientation:
            by_orient = [p for p in chosen if p.get("orientation") == orientation]
            chosen = by_orient or chosen
        idx = _stable_index(app_name, use_case, len(chosen))
        return (cat, chosen[idx]["filename"])
    return None


def stock_photo_url(filename: str) -> str:
    """Public path a build references for a copied stock photo."""
    return f"/images/stock/{filename}"


@lru_cache(maxsize=512)
def stock_photo_source_path(filename: str) -> Optional[Path]:
    """Resolve a manifest filename to its committed source file. Manifest
    filenames are unique across categories; search the declared category first,
    then glob as a last resort. Returns None when the file is absent (the
    manifest lists ~2x the committed files). Cached: the selector calls this
    once per candidate, and the files are static during a build."""
    # The name can come from a spec setting ("/images/stock/<name>"). It is
    # used in a glob pattern and as an output file name, so accept a plain
    # file name only: no separators, no "..", no glob metacharacters.
    if not filename or not _STOCK_FILENAME_RE.fullmatch(filename) or ".." in filename:
        return None
    manifest = _load_manifest()
    for cat, photos in manifest.items():
        for photo in (photos or []):
            if isinstance(photo, dict) and photo.get("filename") == filename:
                cand = _STOCK_DIR / cat / filename
                if cand.exists():
                    return cand
    # Sorted: glob order is filesystem-dependent, and the same name in two
    # categories must resolve to the same file on every machine.
    hits = sorted(_STOCK_DIR.glob(f"*/{filename}"))
    return hits[0] if hits else None


def default_hero_image_url(archetype: str, app_name: str, mood_text: Optional[str] = None) -> str:
    """Curated hero image path, or '' (text-forward hero) when none matches.
    mood_text (keeper description + mood_keywords) steers coach imagery
    business-vs-wellness."""
    sel = select_stock_photo(archetype, "hero", app_name, orientation="landscape", mood_text=mood_text)
    return stock_photo_url(sel[1]) if sel else ""


def default_about_photo_url(archetype: str, app_name: str, mood_text: Optional[str] = None) -> str:
    """Curated about image path, never the same photo as the hero (the two
    images should not duplicate). '' when none matches."""
    hero = select_stock_photo(archetype, "hero", app_name, orientation="landscape", mood_text=mood_text)
    exclude = {hero[1]} if hero else set()
    sel = select_stock_photo(archetype, "about", app_name, orientation="portrait", exclude=exclude, mood_text=mood_text)
    return stock_photo_url(sel[1]) if sel else ""
