// Tests for web/src/data/comparison-pages.ts.
//
// Covers:
//  - getComparisonPage: lookup by slug, case-insensitivity, null on miss.
//  - listComparisonPages: shape stability + canonical slug roster.
//  - comparisonPageJsonLd: Article schema with about[] entity bindings.
//  - comparisonHubJsonLd: CollectionPage with ItemList of every page.

import { describe, it, expect } from 'vitest';
import {
  getComparisonPage,
  listComparisonPages,
  comparisonPageJsonLd,
  comparisonHubJsonLd,
} from '../../src/data/comparison-pages';

const EXPECTED_SLUGS = ['squarespace', 'mindbody', 'wix', 'acuity'];

describe('listComparisonPages', () => {
  it('returns the canonical roster of comparison pages', () => {
    const slugs = listComparisonPages().map((p) => p.slug);
    expect(slugs).toEqual(EXPECTED_SLUGS);
  });

  it('every page has a fully-populated shape', () => {
    for (const page of listComparisonPages()) {
      expect(page.slug).toBeTruthy();
      expect(page.competitorName).toBeTruthy();
      expect(page.competitorUrl).toMatch(/^https?:\/\//);
      expect(page.pageTitle.length).toBeGreaterThan(20);
      expect(page.metaDescription.length).toBeGreaterThan(80);
      expect(page.metaDescription.length).toBeLessThan(320);
      expect(page.audience).toBeTruthy();
      expect(page.tagline).toBeTruthy();
      expect(page.lead.length).toBeGreaterThan(120);
      expect(page.sections.length).toBeGreaterThanOrEqual(3);
      expect(page.features.length).toBeGreaterThanOrEqual(4);
      expect(page.closingPrompt).toBeTruthy();
    }
  });

  it('section + lead body copy stays affirmative (no banned phrases)', () => {
    const banned = [
      /\bif we ever (?:close|shut)\b/i,
      /\bhostage\b/i,
      /\block[- ]?in\b/i,
      /\b7[- ]?days?\b/i,
      /\btrial period\b/i,
      /\bversus\b/i,
      /\bswitching from\b/i,
    ];
    for (const page of listComparisonPages()) {
      const body = [page.lead, page.closingPrompt, ...page.sections.map((s) => s.body)].join('\n');
      for (const pattern of banned) {
        expect(body).not.toMatch(pattern);
      }
    }
  });

  // The pre-commit brand-voice hook does not scan web/src/data (see the
  // header of comparison-pages.ts), so the rules it enforces on visible copy
  // are checked here, on every visible field and against every competitor the
  // pages name: no em dash, and no competitor in the body (a competitor may be
  // named only in the title, the meta description, the URL and the JSON-LD).
  it('the visible body copy has no em dash and names no competitor', () => {
    const pages = listComparisonPages();
    const competitors = pages.map((page) => page.competitorName.toLowerCase());
    expect(competitors.length).toBeGreaterThan(1);
    const emDash = String.fromCharCode(0x2014);
    for (const page of pages) {
      const visible = [
        page.tagline,
        page.lead,
        page.closingPrompt,
        ...page.sections.flatMap((s) => [s.heading, s.body]),
        ...page.features.flatMap((f) => [f.label, f.description]),
      ];
      for (const text of visible) {
        expect(text, `${page.slug}: ${text.slice(0, 40)}`).not.toContain(emDash);
        expect(text.toLowerCase(), `${page.slug}: ${text.slice(0, 40)}`).not.toContain('&mdash;');
        for (const competitor of competitors) {
          expect(text.toLowerCase(), `${page.slug} names ${competitor}: ${text.slice(0, 40)}`).not.toContain(competitor);
        }
      }
    }
  });
});

describe('getComparisonPage', () => {
  it('returns the page for a known slug', () => {
    const page = getComparisonPage('squarespace');
    expect(page).not.toBeNull();
    expect(page!.slug).toBe('squarespace');
  });

  it('lowercases the input before lookup', () => {
    const page = getComparisonPage('SQUARESPACE');
    expect(page).not.toBeNull();
    expect(page!.slug).toBe('squarespace');
  });

  it('returns null for an unknown slug', () => {
    expect(getComparisonPage('unknown')).toBeNull();
  });

  it('returns null for empty / non-string input', () => {
    expect(getComparisonPage('')).toBeNull();
    expect(getComparisonPage(undefined as unknown as string)).toBeNull();
    expect(getComparisonPage(null as unknown as string)).toBeNull();
  });
});

describe('comparisonPageJsonLd', () => {
  it('produces an Article node with the canonical @id and url', () => {
    const page = getComparisonPage('squarespace')!;
    const node = comparisonPageJsonLd(page);
    expect(node['@type']).toBe('Article');
    expect(node['@id']).toBe('https://craftandship.io/vs/squarespace#article');
    expect(node.url).toBe('https://craftandship.io/vs/squarespace');
    expect(node.headline).toBe(page.pageTitle);
    expect(node.description).toBe(page.metaDescription);
  });

  it('binds Author -> Ocean Person and Publisher -> Organization by @id', () => {
    const page = getComparisonPage('mindbody')!;
    const node = comparisonPageJsonLd(page);
    expect(node.author).toEqual({ '@id': 'https://craftandship.io/about/ocean#person' });
    expect(node.publisher).toEqual({ '@id': 'https://craftandship.io#organization' });
    expect(node.isPartOf).toEqual({ '@id': 'https://craftandship.io#organization' });
  });

  it('about[] references Craft & Ship Org plus the named competitor Org', () => {
    const page = getComparisonPage('wix')!;
    const node = comparisonPageJsonLd(page);
    const about = node.about as Array<Record<string, unknown>>;
    expect(about).toHaveLength(2);
    expect(about[0]).toEqual({ '@id': 'https://craftandship.io#organization' });
    expect(about[1]['@type']).toBe('Organization');
    expect(about[1].name).toBe(page.competitorName);
    expect(about[1].url).toBe(page.competitorUrl);
  });
});

describe('comparisonHubJsonLd', () => {
  it('produces a CollectionPage with one ItemList entry per comparison page', () => {
    const node = comparisonHubJsonLd();
    expect(node['@type']).toBe('CollectionPage');
    expect(node['@id']).toBe('https://craftandship.io/vs#collection');
    expect(node.url).toBe('https://craftandship.io/vs');
    const main = node.mainEntity as { '@type': string; itemListElement: Array<{ position: number; url: string }> };
    expect(main['@type']).toBe('ItemList');
    expect(main.itemListElement.length).toBe(EXPECTED_SLUGS.length);
    main.itemListElement.forEach((item, idx) => {
      expect(item.position).toBe(idx + 1);
      expect(item.url).toBe(`https://craftandship.io/vs/${EXPECTED_SLUGS[idx]}`);
    });
  });
});
