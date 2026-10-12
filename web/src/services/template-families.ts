/**
 * Template-family registry + scorer (TS side).
 *
 * Mirrors `compose.intake.pick_template_family()` so the
 * `/lighthouse/preview` UI can score families against a keeper's
 * harbor_meta WITHOUT shelling out to Python. The compose-side picker
 * is still authoritative at build time; this is purely for the
 * customer-facing picker UI which runs in the web tier.
 *
 * Source of truth for templates: compose/data/templates.json. The web
 * tier does not read that file at run time: a vendored copy
 * (src/data/templates.json) is bundled at build time, and
 * CAS_TEMPLATES_JSON_PATH can point at another file (a test hook). If
 * the override is unreadable, an empty list is returned and the picker
 * UI shows "no families available."
 *
 * Contract:
 *   - listFamilies()                          -> all families
 *   - listCompatibleFamilies(archetype)       -> families compat
 *   - scoreFamily(family, harbor_meta)        -> integer score
 *   - pickTopK(archetype, harbor_meta, k)     -> top-k by score, with
 *                                                 'default'-tag tie-break
 *                                                 when scoring is all 0
 *
 * Pure functions; deterministic. Same input -> same output.
 */
import * as fs from 'fs';
// Vendored copy of compose/data/templates.json. Bundled into the
// compiled web JS at build time (resolveJsonModule:true in tsconfig)
// so the prod web container (which does NOT ship /compose/) can still
// read the registry. The fs-based path resolution below is the
// fallback / dev-loop path; in prod the bundled JSON wins.
//
// A test (services.template-families.test.ts: 'vendored copy matches
// compose source-of-truth') asserts the two stay in sync. When
// updating templates.json, edit the compose/data/ copy and re-copy
// into web/src/data/.
import vendoredTemplates from '../data/templates.json';

export interface TemplateFamily {
  id: string;
  name: string;
  summary: string;
  thumbnail_alt_summary?: string;
  compatible_archetypes: string[];
  shell_nav_id: string;
  shell_footer_id: string;
  layout_overrides: Record<string, string>;
  typography_scale: {
    base_rem: number;
    h1_rem: number;
    h2_rem: number;
    h3_rem?: number;
    section_y_rem: number;
    letter_spacing_h1?: string;
    leading_h1?: number;
    leading_body?: number;
  };
  accent_density: 'none' | 'low' | 'medium' | 'high';
  tags: string[];
  complexity_band: 'simple' | 'medium' | 'rich';
  extra_body_css?: string;
}

export interface HarborMetaSubset {
  template_family_hint?: string;
  mood_keywords?: string[];
  hero_style_hint?: string;
  density_hint?: string;
  avoid_keywords?: string[];
  complexity_hint?: 'simple' | 'medium' | 'rich';
}

let _cachedTemplates: TemplateFamily[] | null = null;

/** Test-only: invalidate the in-memory cache. */
export function _resetTemplateCacheForTests(): void {
  _cachedTemplates = null;
}

export function listFamilies(): TemplateFamily[] {
  if (_cachedTemplates !== null) return _cachedTemplates;
  // Override path takes precedence (used by tests to point at a bogus
  // file to verify graceful empty-state).
  const override = process.env.CAS_TEMPLATES_JSON_PATH;
  if (override) {
    try {
      const raw = fs.readFileSync(override, 'utf8');
      const parsed = JSON.parse(raw);
      const list = Array.isArray(parsed.templates) ? parsed.templates : [];
      _cachedTemplates = list as TemplateFamily[];
      return _cachedTemplates;
    } catch (err) {
      console.warn('[template-families] override path unreadable:', err);
      _cachedTemplates = [];
      return _cachedTemplates;
    }
  }
  // Default: use the vendored JSON imported at module-load time.
  // resolveJsonModule:true in tsconfig bundles this into the compiled
  // web JS so the prod container doesn't need /compose/ on disk.
  const list = Array.isArray((vendoredTemplates as { templates?: unknown[] }).templates)
    ? ((vendoredTemplates as { templates: TemplateFamily[] }).templates)
    : [];
  _cachedTemplates = list;
  return _cachedTemplates;
}

export function listCompatibleFamilies(archetype: string): TemplateFamily[] {
  return listFamilies().filter((f) => {
    const compat = f.compatible_archetypes || [];
    return compat.includes('*') || compat.includes(archetype);
  });
}

export function scoreFamily(family: TemplateFamily, meta: HarborMetaSubset): number {
  if (!meta || typeof meta !== 'object') return 0;

  const familyTerms = new Set<string>();
  for (const t of family.tags || []) {
    if (typeof t === 'string') familyTerms.add(t.trim().toLowerCase());
  }
  if (family.complexity_band) familyTerms.add(family.complexity_band.toLowerCase());
  if (family.accent_density) familyTerms.add(family.accent_density.toLowerCase());

  const keeperTerms = new Set<string>();
  for (const t of meta.mood_keywords || []) {
    if (typeof t === 'string') keeperTerms.add(t.trim().toLowerCase());
  }
  if (typeof meta.hero_style_hint === 'string') {
    keeperTerms.add(meta.hero_style_hint.trim().toLowerCase());
  }
  if (typeof meta.density_hint === 'string') {
    keeperTerms.add(meta.density_hint.trim().toLowerCase());
  }

  const avoidTerms = new Set<string>();
  for (const t of meta.avoid_keywords || []) {
    if (typeof t === 'string') avoidTerms.add(t.trim().toLowerCase());
  }

  let score = 0;
  for (const k of keeperTerms) if (familyTerms.has(k)) score += 3;
  for (const a of avoidTerms) if (familyTerms.has(a)) score -= 5;
  if (
    typeof meta.complexity_hint === 'string' &&
    meta.complexity_hint.trim().toLowerCase() ===
      (family.complexity_band || '').trim().toLowerCase()
  ) {
    score += 5;
  }
  return score;
}

/**
 * Stable non-crypto index. Mirrors `_stable_index()` in
 * `compose/intake.py` but in TS. Same `(seed, salt)` pair returns the
 * same index across environments. Uses md5; non-security boundary.
 */
function stableIndex(seed: string, salt: string, modulo: number): number {
  if (modulo <= 0) return 0;
  const crypto = require('crypto') as typeof import('crypto');
  const digest = crypto
    .createHash('md5')
    .update(`${seed || 'default'}|${salt || ''}`, 'utf8')
    .digest();
  // Take first 8 bytes as little-endian unsigned int (mirror Python).
  let n = 0n;
  for (let i = 7; i >= 0; i--) {
    n = (n << 8n) | BigInt(digest[i]);
  }
  return Number(n % BigInt(modulo));
}

export interface ScoredFamily {
  family: TemplateFamily;
  score: number;
}

/**
 * Pick the top-k families for an archetype, scored against
 * harbor_meta. Sorted by score DESC, with deterministic md5 tie-break
 * by `family.id`. When score is all-zero (no signal), the
 * `tags: ['default']` family is preferred so unsignaled keepers see
 * the stable default first (mirrors the Python picker contract).
 *
 * Honors `harbor_meta.template_family_hint` as the strongest signal:
 * the hinted family (if archetype-compatible) is forced to position 0
 * regardless of score. The remaining slots fill from scoring.
 */
export function pickTopK(
  archetype: string,
  meta: HarborMetaSubset | null | undefined,
  k: number,
  seed: string = 'default',
): ScoredFamily[] {
  const candidates = listCompatibleFamilies(archetype);
  if (candidates.length === 0) return [];
  const safeMeta: HarborMetaSubset = meta && typeof meta === 'object' ? meta : {};

  const scored: ScoredFamily[] = candidates.map((f) => ({
    family: f,
    score: scoreFamily(f, safeMeta),
  }));

  // If hint names a real candidate, force it to the front.
  let pinned: ScoredFamily | null = null;
  const rawHint = safeMeta.template_family_hint;
  if (typeof rawHint === 'string' && rawHint.trim()) {
    const hint = rawHint.trim().toLowerCase();
    const found = scored.find((s) => (s.family.id || '').toLowerCase() === hint);
    if (found) pinned = found;
  }

  const rest = scored.filter((s) => s !== pinned);

  // Sort: score DESC, then by deterministic stableIndex on (seed, id).
  rest.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    // tie: prefer 'default'-tagged when scoring is all-zero so unsignaled
    // keepers see classic-business first
    if (a.score === 0) {
      const aDefault = (a.family.tags || []).includes('default') ? 1 : 0;
      const bDefault = (b.family.tags || []).includes('default') ? 1 : 0;
      if (aDefault !== bDefault) return bDefault - aDefault;
    }
    return stableIndex(seed, `template-family:${archetype}:${a.family.id}`, 1000) -
           stableIndex(seed, `template-family:${archetype}:${b.family.id}`, 1000);
  });

  const result = pinned ? [pinned, ...rest] : rest;
  return result.slice(0, k);
}
