"""Self-host Google Fonts pattern for the v2 compose engine.

Eliminates the Google Fonts CSS round-trip that causes fonts to render
only on the second refresh in local dev (and slowly on first load in
prod). The pattern:

  1. Pre-fetch the latin-subset WOFF2 from fonts.gstatic.com per family +
     weight (one-time, offline) and stash them under
     `compose/data/fonts/<family-slug>-<weight>.woff2`.
  2. At compose time, when `_harbor_meta.self_host_fonts: true` is set,
     copy the cached WOFF2s into the build's `public/fonts/` directory
     and inject:
        - one `@font-face` rule per (family, weight) pointing at
          `/fonts/...woff2` with `font-display: swap`, AND
        - `<link rel="preload" as="font" type="font/woff2" crossorigin>`
          tags for the most-used display + body weight (400 by default,
          falling back to the family's first declared weight if 400 isn't
          available).
  3. Suppress the runtime `<link href="fonts.googleapis.com/...">` so the
     browser never makes the round-trip.

The cache is read-only at compose time; populate it by hand (see
`compose/data/fonts/README.md`). When the cache is empty
or missing a needed WOFF2, `plan_self_hosted_fonts()` returns a
`fallback_reason` and the caller (emit_layout_files) falls back to the
classic Google Fonts URL path and records the `self_host_fonts_fallback`
engine warning.

All public functions are pure: same input -> same output, no network,
no file writes outside the caller's responsibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Compose-time cache location. Override via `cache_dir=` kwarg in tests.
DEFAULT_FONT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "fonts"

# Italic variants are pre-fetched as `<slug>-<weight>i.woff2` (the trailing
# "i" marks the italic file). Only upright weights are baked;
# italic support can be layered on later without changing
# the public API (callers opt-in by extending font_pair["body"]["weights"]
# entries to dict-shape {weight, italic}).
WOFF2_EXTENSION = ".woff2"


# ---------------------------------------------------------------------------
# Slug + filename derivation
# ---------------------------------------------------------------------------

def family_slug(family: str) -> str:
    """Convert a Google Fonts display family name into the WOFF2 file slug.

    "Cormorant Garamond" -> "cormorant-garamond"
    "DM Serif Display"   -> "dm-serif-display"
    "IBM Plex Serif"     -> "ibm-plex-serif"

    The slug matches the WOFF2 filename stem; pair it with
    `<slug>-<weight>.woff2` to compute the cache filename.
    """
    if not family:
        return ""
    s = family.strip().lower()
    # Collapse runs of non-alnum into single dashes; drop leading/trailing.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def font_filename(family: str, weight: int) -> str:
    """`Cormorant Garamond` + 400 -> `cormorant-garamond-400.woff2`."""
    return f"{family_slug(family)}-{int(weight)}{WOFF2_EXTENSION}"


# ---------------------------------------------------------------------------
# Spec opt-in helpers
# ---------------------------------------------------------------------------

def is_self_host_enabled(harbor_meta: Optional[dict]) -> bool:
    """Read `_harbor_meta.self_host_fonts` defensively.

    Returns True only when the key is the literal boolean True. String
    "true" / "false" deliberately don't pass; the harbor translator
    normalizes the field upstream, so anything non-bool reaching here is
    treated as a typo and falls back to the Google Fonts URL.
    """
    if not isinstance(harbor_meta, dict):
        return False
    return harbor_meta.get("self_host_fonts") is True


# ---------------------------------------------------------------------------
# Font-pair plan
# ---------------------------------------------------------------------------

@dataclass
class FontFilePlan:
    """One (family, weight) entry that needs a WOFF2."""
    family: str
    weight: int
    cache_path: Path
    public_path: str  # e.g. "public/fonts/cormorant-garamond-400.woff2"

    @property
    def public_url(self) -> str:
        """The runtime URL the browser fetches: `/fonts/<file>.woff2`."""
        return "/" + self.public_path[len("public/"):]


@dataclass
class SelfHostFontsPlan:
    """Output of `plan_self_hosted_fonts()`.

    `files` is the (family, weight) cache list that's present + ready to
    bake. `missing` is the list that the cache couldn't resolve, when
    non-empty, callers MUST fall back to the Google Fonts URL because a
    partial bake would render some weights but not others.
    """
    files: List[FontFilePlan] = field(default_factory=list)
    missing: List[Tuple[str, int]] = field(default_factory=list)
    preload_families: List[str] = field(default_factory=list)
    # When set, callers should fall back to the Google Fonts URL and
    # record an engine warning. Cleared on the happy path.
    fallback_reason: Optional[str] = None


def _weights_for(slot: Optional[dict]) -> List[int]:
    """Extract numeric weights from a font_pair slot, tolerating both
    plain ints (`weights: [400, 700]`) and dict shape (future-proofing
    for italic). Drops anything non-numeric defensively."""
    if not isinstance(slot, dict):
        return []
    out: List[int] = []
    for w in slot.get("weights") or []:
        if isinstance(w, bool):
            continue  # bool is a subclass of int; exclude explicitly
        if isinstance(w, int):
            out.append(w)
        elif isinstance(w, dict):
            wv = w.get("weight")
            if isinstance(wv, int) and not isinstance(wv, bool):
                out.append(wv)
    return out


def _preload_weight(weights: List[int]) -> Optional[int]:
    """Pick the weight a browser should preload eagerly.

    Prefer 400 (regular body / display) since it's the most common
    first-paint glyph set. Fall back to the family's first declared
    weight when 400 isn't loaded.
    """
    if not weights:
        return None
    if 400 in weights:
        return 400
    return weights[0]


def plan_self_hosted_fonts(
    font_pair: dict,
    cache_dir: Optional[Path] = None,
) -> SelfHostFontsPlan:
    """Build the per-WOFF2 cache plan for a font pair.

    Reads `compose/data/fonts/<family-slug>-<weight>.woff2` (or the
    `cache_dir` override). Returns a plan with:
      - `.files`: list of FontFilePlan entries that are CACHED and ready
      - `.missing`: list of (family, weight) entries the cache lacks
      - `.preload_families`: display + body family names for `<link rel=preload>`
      - `.fallback_reason`: set when caller should NOT self-host

    Determinism: same `font_pair` + cache contents always produces the
    same plan; order matches font_pair["display"]["weights"] first, then
    font_pair["body"]["weights"], with cross-slot weight dedup so a family
    that appears in both slots (e.g. inter-inter) doesn't emit duplicate
    @font-face rules.
    """
    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_FONT_CACHE_DIR
    plan = SelfHostFontsPlan()

    if not isinstance(font_pair, dict):
        plan.fallback_reason = "font_pair is not a dict"
        return plan

    display = font_pair.get("display") or {}
    body = font_pair.get("body") or {}
    display_family = (display.get("family") or "").strip()
    body_family = (body.get("family") or "").strip()

    if not display_family or not body_family:
        plan.fallback_reason = "font_pair missing display/body family"
        return plan

    # Collect (family, weight) pairs. Same-family-different-slot rows
    # (e.g. Inter in both display + body for "inter-inter") merge their
    # weight lists and dedupe.
    family_weights: Dict[str, List[int]] = {}
    for fam, slot in ((display_family, display), (body_family, body)):
        wl = _weights_for(slot)
        if not wl:
            continue
        bucket = family_weights.setdefault(fam, [])
        for w in wl:
            if w not in bucket:
                bucket.append(w)

    if not family_weights:
        plan.fallback_reason = "font_pair declares no weights"
        return plan

    # Walk families + weights in deterministic order (display first).
    seen_keys = set()
    ordered_families: List[str] = []
    for fam in (display_family, body_family):
        if fam in family_weights and fam not in ordered_families:
            ordered_families.append(fam)

    for fam in ordered_families:
        for w in family_weights[fam]:
            key = (fam, w)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            fname = font_filename(fam, w)
            cpath = cache_dir / fname
            ppath = f"public/fonts/{fname}"
            if not cpath.is_file():
                plan.missing.append(key)
            else:
                plan.files.append(FontFilePlan(
                    family=fam, weight=w, cache_path=cpath, public_path=ppath
                ))

    if plan.missing:
        miss = ", ".join(f"{f}@{w}" for f, w in plan.missing)
        plan.fallback_reason = f"WOFF2 cache missing entries: {miss}"
        # Discard files too: partial bake renders wrong (browser shows
        # fallback for missing weights while baked weights paint).
        plan.files = []

    # Preload set: display + body family (deduped). Even when both slots
    # share a family (inter-inter), one preload is enough.
    preload_seen = set()
    for fam in ordered_families:
        if fam in preload_seen:
            continue
        preload_seen.add(fam)
        plan.preload_families.append(fam)

    return plan


# ---------------------------------------------------------------------------
# HTML / CSS rendering
# ---------------------------------------------------------------------------

def render_preload_links(plan: SelfHostFontsPlan) -> str:
    """`<link rel="preload">` tags for the preload weight of each family.

    The preload makes the browser fetch the WOFF2 in parallel with the
    HTML parse instead of waiting for the CSSOM to discover the @font-face
    rule. Required to actually eliminate the "first paint shows fallback"
    flash. Only the preload weight per family is preloaded; loading every
    weight eagerly hurts network priority for above-the-fold paint.
    """
    if not plan.files:
        return ""
    by_family: Dict[str, List[FontFilePlan]] = {}
    for f in plan.files:
        by_family.setdefault(f.family, []).append(f)

    lines: List[str] = []
    for fam in plan.preload_families:
        bucket = by_family.get(fam) or []
        weights = [f.weight for f in bucket]
        pw = _preload_weight(weights)
        if pw is None:
            continue
        match = next((f for f in bucket if f.weight == pw), None)
        if not match:
            continue
        lines.append(
            f'<link rel="preload" as="font" type="font/woff2" '
            f'href="{match.public_url}" crossorigin>'
        )
    return "\n  ".join(lines)


def render_font_face_css(plan: SelfHostFontsPlan) -> str:
    """One `@font-face` rule per WOFF2 file in the plan.

    `font-display: swap` matches Google Fonts' default behavior, render
    fallback immediately, swap to webfont when loaded, so we don't
    regress to FOIT. `unicode-range` is intentionally omitted: we serve
    the latin subset only, so the family
    handles its whole declared range without a separate subset rule.
    """
    if not plan.files:
        return ""
    out: List[str] = []
    for f in plan.files:
        out.append(
            "@font-face {\n"
            f"  font-family: '{f.family}';\n"
            "  font-style: normal;\n"
            f"  font-weight: {f.weight};\n"
            "  font-display: swap;\n"
            f"  src: url('{f.public_url}') format('woff2');\n"
            "}"
        )
    return "\n".join(out)


def emit_font_files(plan: SelfHostFontsPlan) -> List[Tuple[str, bytes]]:
    """Return list of (public-path, bytes) tuples for the cached WOFF2s.

    Each entry is ready for compose's emit pipeline (path under public/,
    raw bytes). Cache files that read empty (0-byte placeholder) are
    skipped defensively to avoid emitting broken downloads; callers
    should treat an empty result as "fall back to Google Fonts."
    """
    out: List[Tuple[str, bytes]] = []
    for f in plan.files:
        try:
            data = f.cache_path.read_bytes()
        except (OSError, IOError):
            continue
        if not data:
            continue
        out.append((f.public_path, data))
    return out
