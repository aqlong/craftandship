/**
 * Tests for web/src/services/template-families.ts.
 *
 * Mirrors the Python `pick_template_family` contract from
 * compose/intake.py. The TS scorer is purely for the customer-facing
 * `/lighthouse/preview` UI; the compose-side picker is authoritative
 * at build time.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as path from 'path';

// Use the real templates.json (compose/data/templates.json) via the
// repo-root resolution baked into the service.
import {
  listFamilies,
  listCompatibleFamilies,
  scoreFamily,
  pickTopK,
  _resetTemplateCacheForTests,
  type TemplateFamily,
  type HarborMetaSubset,
} from '../../src/services/template-families';

beforeEach(() => {
  _resetTemplateCacheForTests();
  delete process.env.CAS_TEMPLATES_JSON_PATH;
});

describe('listFamilies', () => {
  it('loads all starter families from the vendored templates.json', () => {
    const families = listFamilies();
    const ids = families.map((f) => f.id).sort();
    expect(ids).toEqual([
      'classic-business',
      'directory-rich',
      'editorial-sanctuary',
      'editorial-statement',
      'event-led',
      'simple-statement',
    ]);
  });

  it('returns empty array gracefully when override path is missing', () => {
    process.env.CAS_TEMPLATES_JSON_PATH = '/tmp/does-not-exist-templates.json';
    _resetTemplateCacheForTests();
    expect(listFamilies()).toEqual([]);
  });

  it('caches the parsed file across calls', () => {
    const a = listFamilies();
    const b = listFamilies();
    expect(a).toBe(b); // same array reference
  });

  it('every family declares thumbnail_alt_summary for screen-reader users', () => {
    // Drives the /lighthouse/preview alt text. Every family must carry
    // one short sentence describing visual character so screen-reader
    // users get the structural decision aid sighted users see in the
    // specs list.
    const families = listFamilies();
    expect(families.length).toBeGreaterThan(0);
    for (const f of families) {
      expect(typeof f.thumbnail_alt_summary).toBe('string');
      const s = (f.thumbnail_alt_summary || '').trim();
      // Non-empty, short sentence (cap at ~200 chars; no display rule
      // is forgiving enough for a paragraph here).
      expect(s.length).toBeGreaterThan(10);
      expect(s.length).toBeLessThanOrEqual(200);
      // Brand voice: no em dashes.
      expect(s).not.toContain('\u2014');
    }
  });

  it('vendored web/src/data/templates.json matches compose/data/templates.json source-of-truth', () => {
    // Drift guard: the vendored copy that gets bundled into the prod
    // web container MUST equal the compose-side source-of-truth at all
    // times. When updating templates.json, edit the compose/data/ copy
    // and `cp ../compose/data/templates.json web/src/data/templates.json`.
    // This test fails on any drift.
    const fs = require('fs') as typeof import('fs');
    const path = require('path') as typeof import('path');
    const composeRoot = path.resolve(__dirname, '..', '..', '..', 'compose', 'data', 'templates.json');
    const webVendored = path.resolve(__dirname, '..', '..', 'src', 'data', 'templates.json');
    const composeContent = JSON.parse(fs.readFileSync(composeRoot, 'utf8'));
    const webContent = JSON.parse(fs.readFileSync(webVendored, 'utf8'));
    expect(webContent).toEqual(composeContent);
  });
});

describe('listCompatibleFamilies', () => {
  it('classic-business compatible with every archetype (wildcard)', () => {
    const out = listCompatibleFamilies('healer-wellness');
    expect(out.some((f) => f.id === 'classic-business')).toBe(true);
  });

  it('editorial-statement compatible with healer/coach/therapist only', () => {
    const compat = listCompatibleFamilies('healer-wellness').map((f) => f.id);
    expect(compat).toContain('editorial-statement');
    const incompat = listCompatibleFamilies('fitness-studio').map((f) => f.id);
    expect(incompat).not.toContain('editorial-statement');
  });

  it('directory-rich compatible with tradesperson + local-service', () => {
    expect(
      listCompatibleFamilies('tradesperson-contractor').map((f) => f.id)
    ).toContain('directory-rich');
    expect(
      listCompatibleFamilies('local-service').map((f) => f.id)
    ).toContain('directory-rich');
  });

  it('event-led compatible with event-producer + fitness + restaurant', () => {
    expect(listCompatibleFamilies('event-producer').map((f) => f.id)).toContain('event-led');
    expect(listCompatibleFamilies('fitness-studio').map((f) => f.id)).toContain('event-led');
    expect(listCompatibleFamilies('restaurant-cafe').map((f) => f.id)).toContain('event-led');
    expect(listCompatibleFamilies('healer-wellness').map((f) => f.id)).not.toContain('event-led');
  });

  it('returns wildcard-only families for unknown archetype', () => {
    // unknown archetype still gets wildcards (classic-business)
    const out = listCompatibleFamilies('does-not-exist').map((f) => f.id);
    expect(out).toEqual(['classic-business']);
  });
});

describe('scoreFamily', () => {
  it('mood-keyword overlap awards +3 each', () => {
    const editorial = listFamilies().find((f) => f.id === 'editorial-statement')!;
    const meta: HarborMetaSubset = {
      mood_keywords: ['airy', 'editorial', 'quiet', 'minimal'],
    };
    // editorial tags include airy, editorial, quiet, literary, minimal, statement
    // meta keywords overlap on airy, editorial, quiet, minimal -> 4 * 3 = 12
    expect(scoreFamily(editorial, meta)).toBe(12);
  });

  it('avoid_keywords penalize -5 each', () => {
    const editorial = listFamilies().find((f) => f.id === 'editorial-statement')!;
    const meta: HarborMetaSubset = { avoid_keywords: ['airy', 'editorial'] };
    expect(scoreFamily(editorial, meta)).toBe(-10);
  });

  it('complexity_hint match awards +5 (only; mood_keywords not contributing here)', () => {
    const simple = listFamilies().find((f) => f.id === 'simple-statement')!;
    const meta: HarborMetaSubset = { complexity_hint: 'simple' };
    // complexity_band='simple' matches complexity_hint -> +5. No
    // mood_keywords were supplied so the +3-per-overlap path doesn't
    // contribute. (Scoring NEVER double-counts complexity_hint as a
    // mood term; the complexity bonus is its own +5 branch.)
    expect(scoreFamily(simple, meta)).toBe(5);
  });

  it('returns 0 for empty meta', () => {
    const f = listFamilies()[0];
    expect(scoreFamily(f, {})).toBe(0);
  });

  it('handles non-object meta defensively', () => {
    const f = listFamilies()[0];
    expect(scoreFamily(f, null as unknown as HarborMetaSubset)).toBe(0);
  });
});

describe('pickTopK', () => {
  it('explicit hint pins family to position 0 even when it does not score highest', () => {
    const out = pickTopK(
      'healer-wellness',
      { template_family_hint: 'editorial-statement', mood_keywords: [] },
      3,
      'maya'
    );
    expect(out[0].family.id).toBe('editorial-statement');
  });

  it('hint is ignored if family is incompatible with archetype', () => {
    // editorial-statement not compatible with fitness-studio
    const out = pickTopK(
      'fitness-studio',
      { template_family_hint: 'editorial-statement', mood_keywords: ['energetic'] },
      3,
      'studio-1'
    );
    expect(out[0].family.id).not.toBe('editorial-statement');
  });

  it('mood-keyword scoring drives editorial to the top', () => {
    const out = pickTopK(
      'healer-wellness',
      { mood_keywords: ['airy', 'editorial', 'quiet', 'minimal'] },
      3,
      'maya'
    );
    expect(out[0].family.id).toBe('editorial-statement');
  });

  it('no hints -> default-tagged family (classic-business) wins position 0', () => {
    // All candidates score 0; tags:[default] tie-break preferred.
    const out = pickTopK('healer-wellness', {}, 3, 'maya');
    expect(out[0].family.id).toBe('classic-business');
  });

  it('caps at k', () => {
    const out = pickTopK('healer-wellness', {}, 2, 'maya');
    expect(out.length).toBe(2);
  });

  it('determinism: same input -> same output', () => {
    const meta = { mood_keywords: ['quiet'] };
    const a = pickTopK('healer-wellness', meta, 3, 'theo');
    const b = pickTopK('healer-wellness', meta, 3, 'theo');
    expect(a.map((s) => s.family.id)).toEqual(b.map((s) => s.family.id));
  });

  it('returns empty array for archetype with no compatible families', () => {
    process.env.CAS_TEMPLATES_JSON_PATH = '/tmp/does-not-exist-2.json';
    _resetTemplateCacheForTests();
    const out = pickTopK('healer-wellness', {}, 3, 'maya');
    expect(out).toEqual([]);
  });

  it('handles null/undefined meta defensively', () => {
    const a = pickTopK('healer-wellness', null, 3, 'x');
    const b = pickTopK('healer-wellness', undefined, 3, 'x');
    expect(a.length).toBeGreaterThan(0);
    expect(b.length).toBeGreaterThan(0);
  });
});
