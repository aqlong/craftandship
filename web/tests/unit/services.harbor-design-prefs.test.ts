/**
 * Tests for Phase 1 design-preference normalizers + translator wiring
 * (2026-04-28).
 *
 * Coverage:
 *   - normalizeFreeStringHint: registry-id-shaped strings only
 *   - normalizeDensityHint: closed enum
 *   - normalizeHeroStyleHint: closed enum
 *   - normalizeKeywordList: lowercase, trimmed, deduped, capped at 5
 *   - translateExtractedSpecToV1: design-pref fields flow into _harbor_meta
 *
 * The Python side (compose/intake.py) re-validates each hint against
 * the actual compose registries. These TS tests only assert the
 * shape-cleaning contract.
 */
import { describe, it, expect } from 'vitest';
import {
  normalizeFreeStringHint,
  normalizeDensityHint,
  normalizeHeroStyleHint,
  normalizeKeywordList,
  translateExtractedSpecToV1,
  type ExtractedSpecInput,
} from '../../src/services/harbor_to_compose';

describe('normalizeFreeStringHint', () => {
  it('returns lowercase trimmed string for registry-id shape', () => {
    expect(normalizeFreeStringHint('  Sage-Clay  ')).toBe('sage-clay');
    expect(normalizeFreeStringHint('warm-earth')).toBe('warm-earth');
    expect(normalizeFreeStringHint('rubik-nunito')).toBe('rubik-nunito');
  });

  it('rejects non-string', () => {
    expect(normalizeFreeStringHint(123)).toBeUndefined();
    expect(normalizeFreeStringHint(null)).toBeUndefined();
    expect(normalizeFreeStringHint(undefined)).toBeUndefined();
    expect(normalizeFreeStringHint([])).toBeUndefined();
    expect(normalizeFreeStringHint({})).toBeUndefined();
  });

  it('rejects empty / whitespace', () => {
    expect(normalizeFreeStringHint('')).toBeUndefined();
    expect(normalizeFreeStringHint('   ')).toBeUndefined();
  });

  it('rejects values with disallowed characters', () => {
    // Anything that couldn't possibly be a registry id.
    expect(normalizeFreeStringHint('hello world')).toBeUndefined();
    expect(normalizeFreeStringHint('warm earth!')).toBeUndefined();
    expect(normalizeFreeStringHint('a/b')).toBeUndefined();
    expect(normalizeFreeStringHint('-leading-dash')).toBeUndefined();
    expect(normalizeFreeStringHint('a')).toBeUndefined();  // too short
  });

  it('rejects oversized strings', () => {
    expect(normalizeFreeStringHint('a'.repeat(60))).toBeUndefined();
  });

  it('accepts numeric leading char', () => {
    expect(normalizeFreeStringHint('1abc')).toBe('1abc');
  });
});

describe('normalizeDensityHint', () => {
  it('accepts the closed set', () => {
    expect(normalizeDensityHint('airy')).toBe('airy');
    expect(normalizeDensityHint('standard')).toBe('standard');
    expect(normalizeDensityHint('dense')).toBe('dense');
    expect(normalizeDensityHint('  AIRY  ')).toBe('airy');
  });

  it('rejects out-of-set values', () => {
    expect(normalizeDensityHint('cozy')).toBeUndefined();
    expect(normalizeDensityHint('compact')).toBeUndefined();
    expect(normalizeDensityHint('')).toBeUndefined();
    expect(normalizeDensityHint(null)).toBeUndefined();
    expect(normalizeDensityHint(123)).toBeUndefined();
  });
});

describe('normalizeHeroStyleHint', () => {
  it('accepts the closed set', () => {
    expect(normalizeHeroStyleHint('centered')).toBe('centered');
    expect(normalizeHeroStyleHint('split-image')).toBe('split-image');
    expect(normalizeHeroStyleHint('editorial')).toBe('editorial');
    expect(normalizeHeroStyleHint('image-led')).toBe('image-led');
    expect(normalizeHeroStyleHint('  EDITORIAL  ')).toBe('editorial');
  });

  it('rejects out-of-set values', () => {
    expect(normalizeHeroStyleHint('full-bleed')).toBeUndefined();
    expect(normalizeHeroStyleHint('stacked')).toBeUndefined();
    expect(normalizeHeroStyleHint('')).toBeUndefined();
    expect(normalizeHeroStyleHint(null)).toBeUndefined();
  });
});

describe('normalizeKeywordList', () => {
  it('returns empty array for non-list', () => {
    expect(normalizeKeywordList('warm')).toEqual([]);
    expect(normalizeKeywordList(null)).toEqual([]);
    expect(normalizeKeywordList(undefined)).toEqual([]);
    expect(normalizeKeywordList({})).toEqual([]);
  });

  it('lowercases + trims + dedupes', () => {
    expect(normalizeKeywordList(['  Warm ','EARTHY','warm','  '])).toEqual(['warm','earthy']);
  });

  it('caps at 5 entries', () => {
    const out = normalizeKeywordList(['warm','airy','quiet','earthy','minimal','editorial','intimate','cozy']);
    expect(out).toEqual(['warm','airy','quiet','earthy','minimal']);
  });

  it('drops non-string entries', () => {
    expect(normalizeKeywordList(['warm', 1, null, 'earthy'])).toEqual(['warm','earthy']);
  });

  it('rejects single-character entries (treated as junk)', () => {
    expect(normalizeKeywordList(['a','b','warm'])).toEqual(['warm']);
  });

  it('drops malformed strings (uppercase numerics, special chars, too long)', () => {
    expect(normalizeKeywordList([
      'warm',
      '1numeric-start-rejected',
      'has space',
      'has/slash',
      'a'.repeat(25),  // too long
    ])).toEqual(['warm']);
  });

  it('preserves first-seen order across dedupes', () => {
    const out = normalizeKeywordList(['warm','earthy','warm','quiet','earthy','airy']);
    expect(out).toEqual(['warm','earthy','quiet','airy']);
  });
});

describe('translateExtractedSpecToV1 design-pref wiring', () => {
  function baseInput(overrides: Partial<ExtractedSpecInput> = {}): ExtractedSpecInput {
    return {
      app_name: 'design-test',
      business_type: 'reiki practice',
      archetype: 'healer-wellness',
      tier: 'build',
      ...overrides,
    };
  }

  it('flows palette_hint into _harbor_meta', () => {
    const { spec } = translateExtractedSpecToV1(baseInput({
      design: { palette_hint: 'sage-clay' },
    }));
    expect(spec._harbor_meta?.palette_hint).toBe('sage-clay');
  });

  it('flows font_pair_hint into _harbor_meta', () => {
    const { spec } = translateExtractedSpecToV1(baseInput({
      design: { font_pair_hint: 'cormorant-lato' },
    }));
    expect(spec._harbor_meta?.font_pair_hint).toBe('cormorant-lato');
  });

  it('flows density + hero_style into _harbor_meta', () => {
    const { spec } = translateExtractedSpecToV1(baseInput({
      design: { density_hint: 'airy', hero_style_hint: 'editorial' },
    }));
    expect(spec._harbor_meta?.density_hint).toBe('airy');
    expect(spec._harbor_meta?.hero_style_hint).toBe('editorial');
  });

  it('flows mood_keywords + avoid_keywords into _harbor_meta', () => {
    const { spec } = translateExtractedSpecToV1(baseInput({
      design: { mood_keywords: ['warm','earthy','quiet'], avoid_keywords: ['corporate'] },
    }));
    expect(spec._harbor_meta?.mood_keywords).toEqual(['warm','earthy','quiet']);
    expect(spec._harbor_meta?.avoid_keywords).toEqual(['corporate']);
  });

  it('drops malformed hints rather than passing them through', () => {
    const { spec } = translateExtractedSpecToV1(baseInput({
      design: {
        palette_hint: 'not a registry id',
        font_pair_hint: 'has spaces',
        density_hint: 'cozy',
        hero_style_hint: 'panoramic',
        mood_keywords: 'string-not-list',
      },
    }));
    expect(spec._harbor_meta?.palette_hint).toBeUndefined();
    expect(spec._harbor_meta?.font_pair_hint).toBeUndefined();
    expect(spec._harbor_meta?.density_hint).toBeUndefined();
    expect(spec._harbor_meta?.hero_style_hint).toBeUndefined();
    expect(spec._harbor_meta?.mood_keywords).toBeUndefined();
  });

  it('omits design-pref fields entirely when not supplied (no empty arrays)', () => {
    const { spec } = translateExtractedSpecToV1(baseInput());
    // Either no _harbor_meta at all, or _harbor_meta without these keys.
    const meta = spec._harbor_meta;
    if (meta) {
      expect(meta.palette_hint).toBeUndefined();
      expect(meta.font_pair_hint).toBeUndefined();
      expect(meta.mood_keywords).toBeUndefined();
      expect(meta.avoid_keywords).toBeUndefined();
    }
  });

  it('determinism: same input -> same _harbor_meta', () => {
    const input = baseInput({
      design: { palette_hint: 'sage-clay', mood_keywords: ['warm','earthy'] },
    });
    const a = translateExtractedSpecToV1(input);
    const b = translateExtractedSpecToV1(input);
    expect(a.spec._harbor_meta).toEqual(b.spec._harbor_meta);
  });
});
