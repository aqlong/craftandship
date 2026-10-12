// Unit tests for the JSON-LD structured-data helpers (web/src/services/structured-data.ts).
//
// These are pure function tests. The HTTP-level "schema renders into the
// page head" tests live in routes.public-pages.test.ts (Phase 0 SEO block).

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  organizationJsonLd,
  oceanPersonJsonLd,
  faqPageJsonLd,
  jsonLdGraph,
  jsonLdScriptTag,
  parseSameAsList,
} from '../../src/services/structured-data';

describe('organizationJsonLd', () => {
  const org = organizationJsonLd();

  it('declares schema.org context and Organization type', () => {
    expect(org['@context']).toBe('https://schema.org');
    expect(org['@type']).toBe('Organization');
  });

  it('uses a stable canonical @id so other nodes can reference it', () => {
    expect(org['@id']).toBe('https://craftandship.io#organization');
  });

  it('sets canonical name + URL + logo', () => {
    expect(org.name).toBe('Craft & Ship');
    expect(org.url).toBe('https://craftandship.io');
    expect(org.logo).toBe('https://craftandship.io/brand-harbor/favicon.svg');
  });

  it('description is affirmative and forward-looking: no shutdown talk, no competitor framing, no timetable', () => {
    const desc = org.description as string;
    // Affirmative voice: name what we do, not what we are not.
    expect(desc).toMatch(/(?:designs|builds|companion)/i);
    // No defensive / scary framings.
    expect(desc).not.toMatch(/\bif we ever\b|\bnever held\b|\bhostage\b|\bshutdown\b|\block[- ]?in\b/i);
    // No calendar-timetable phrases.
    expect(desc).not.toMatch(/\b7[- ]day\b|\bseven[- ]day\b|\btrial period\b/i);
    // No competitor names on a customer-facing surface (AI engines paraphrase this).
    expect(desc).not.toMatch(/\b(Mindbody|Squarespace|Wix|Practice\.do|Lovable|Bolt)\b/);
  });

  it('founder is a stable @id reference to the Person node', () => {
    expect(org.founder).toEqual({ '@id': 'https://craftandship.io/about/ocean#person' });
  });

  it('alternateName covers spelling variants AI engines may extract', () => {
    expect(org.alternateName).toEqual(expect.arrayContaining(['CraftAndShip', 'Craft and Ship']));
  });

  it('omits sameAs entirely when no profile URLs are configured', () => {
    // Per Schema.org rich-results validators, empty arrays are flagged as
    // "empty value." We keep the key out of the schema until we have at
    // least one URL to put in it.
    const fresh = organizationJsonLd();
    expect(fresh.sameAs).toBeUndefined();
  });

  it('emits sameAs when CAS_ORG_SAMEAS env is set with valid URLs', () => {
    const prev = process.env.CAS_ORG_SAMEAS;
    process.env.CAS_ORG_SAMEAS = 'https://www.linkedin.com/company/example-studio,https://twitter.com/example-studio';
    try {
      const node = organizationJsonLd();
      expect(node.sameAs).toEqual([
        'https://www.linkedin.com/company/example-studio',
        'https://twitter.com/example-studio',
      ]);
    } finally {
      if (prev === undefined) delete process.env.CAS_ORG_SAMEAS;
      else process.env.CAS_ORG_SAMEAS = prev;
    }
  });
});

describe('oceanPersonJsonLd', () => {
  const person = oceanPersonJsonLd();

  it('declares Person type at canonical @id', () => {
    expect(person['@type']).toBe('Person');
    expect(person['@id']).toBe('https://craftandship.io/about/ocean#person');
  });

  it('worksFor references Organization by @id (not by inlining the org)', () => {
    expect(person.worksFor).toEqual({ '@id': 'https://craftandship.io#organization' });
  });

  it('carries no work history (alumniOf is intentionally omitted)', () => {
    expect(person.alumniOf).toBeUndefined();
  });

  it('description is affirmative: no defensive language', () => {
    const desc = person.description as string;
    expect(desc).not.toMatch(/\bif we ever\b|\bhostage\b|\bnever pretend\b/i);
  });

  it('url points at the dedicated /about/ocean page', () => {
    expect(person.url).toBe('https://craftandship.io/about/ocean');
  });

  it('omits sameAs entirely when no profile URLs are configured', () => {
    const fresh = oceanPersonJsonLd();
    expect(fresh.sameAs).toBeUndefined();
  });

  it('emits sameAs when CAS_OCEAN_SAMEAS env is set with valid URLs', () => {
    const prev = process.env.CAS_OCEAN_SAMEAS;
    process.env.CAS_OCEAN_SAMEAS = 'https://www.linkedin.com/in/example-person, https://github.com/example-person';
    try {
      const node = oceanPersonJsonLd();
      expect(node.sameAs).toEqual([
        'https://www.linkedin.com/in/example-person',
        'https://github.com/example-person',
      ]);
    } finally {
      if (prev === undefined) delete process.env.CAS_OCEAN_SAMEAS;
      else process.env.CAS_OCEAN_SAMEAS = prev;
    }
  });

  it('omits sameAs when env is set but every entry is invalid', () => {
    const prev = process.env.CAS_OCEAN_SAMEAS;
    process.env.CAS_OCEAN_SAMEAS = 'not-a-url, ,';
    try {
      const node = oceanPersonJsonLd();
      expect(node.sameAs).toBeUndefined();
    } finally {
      if (prev === undefined) delete process.env.CAS_OCEAN_SAMEAS;
      else process.env.CAS_OCEAN_SAMEAS = prev;
    }
  });
});

describe('parseSameAsList', () => {
  it('returns empty array for empty input', () => {
    expect(parseSameAsList('')).toEqual([]);
  });

  it('splits comma-separated URLs and trims whitespace', () => {
    expect(parseSameAsList('https://a.example , https://b.example,https://c.example')).toEqual([
      'https://a.example/',
      'https://b.example/',
      'https://c.example/',
    ].map(s => s.replace(/\/$/, '')));
    // URL constructor adds a trailing slash for bare hostnames; we feed it
    // hostname-only inputs so accept either form.
  });

  it('drops entries that do not parse as valid URLs', () => {
    expect(parseSameAsList('https://valid.example, not-a-url, , also-bad')).toEqual([
      'https://valid.example',
    ]);
  });

  it('preserves stable input order', () => {
    const input = 'https://z.example,https://a.example,https://m.example';
    expect(parseSameAsList(input)).toEqual([
      'https://z.example',
      'https://a.example',
      'https://m.example',
    ]);
  });
});

describe('faqPageJsonLd', () => {
  it('wraps each FAQ as a Question + Answer', () => {
    const items = [
      { question: 'How does the sliding scale work?', answer: 'Your word is enough.' },
      { question: 'How does billing work?', answer: 'Monthly.' },
    ];
    const node = faqPageJsonLd(items);
    expect(node['@type']).toBe('FAQPage');
    const main = node.mainEntity as Array<{ '@type': string; name: string; acceptedAnswer: { '@type': string; text: string } }>;
    expect(main).toHaveLength(2);
    expect(main[0]['@type']).toBe('Question');
    expect(main[0].name).toBe('How does the sliding scale work?');
    expect(main[0].acceptedAnswer['@type']).toBe('Answer');
    expect(main[0].acceptedAnswer.text).toBe('Your word is enough.');
  });

  it('handles empty list cleanly', () => {
    const node = faqPageJsonLd([]);
    expect(node.mainEntity).toEqual([]);
  });
});

describe('jsonLdGraph', () => {
  it('wraps multiple nodes in @graph with shared schema.org context', () => {
    const a = { '@context': 'https://schema.org', '@type': 'Organization', name: 'A' };
    const b = { '@context': 'https://schema.org', '@type': 'Person', name: 'B' };
    const graph = jsonLdGraph([a, b]);
    expect(graph['@context']).toBe('https://schema.org');
    expect(graph['@graph']).toHaveLength(2);
    // Inner @context is stripped to avoid redundant nesting.
    const inner = graph['@graph'] as Array<Record<string, unknown>>;
    expect(inner[0]['@context']).toBeUndefined();
    expect(inner[0]['@type']).toBe('Organization');
    expect(inner[1]['@type']).toBe('Person');
  });
});

describe('jsonLdScriptTag', () => {
  it('renders a script tag with application/ld+json mime type', () => {
    const tag = jsonLdScriptTag({ '@context': 'https://schema.org', '@type': 'Organization', name: 'X' });
    expect(tag).toMatch(/^<script type="application\/ld\+json">/);
    expect(tag).toMatch(/<\/script>$/);
  });

  // Body of the tag, without the wrapper this module writes itself.
  const bodyOf = (tag: string): string =>
    tag.replace(/^<script type="application\/ld\+json"[^>]*>/, '').replace(/<\/script>$/, '');

  it('leaves no "<" or ">" in the body, whatever the string values contain', () => {
    const hostile = [
      'hello </script><img src=x onerror=alert(1)>',
      // The HTML parser matches the end tag case-insensitively and accepts
      // whitespace or a slash after the name; a "</script>" pattern misses these.
      '</SCRIPT >',
      '</script\t>',
      '</script/>',
      '</scr' + 'ipt foo="bar">',
      // Comment plus nested script start: puts the parser in the
      // double-escaped state, where the real end tag is swallowed.
      '<!--<script>',
      '-->',
    ];
    for (const payload of hostile) {
      const node = { '@type': 'Thing', payload };
      const body = bodyOf(jsonLdScriptTag(node));
      expect(body).not.toContain('<');
      expect(body).not.toContain('>');
      // A JSON consumer still reads the original value back.
      expect(JSON.parse(body)).toEqual(node);
    }
  });

  it('escapes the JavaScript line separators U+2028 and U+2029', () => {
    const node = { '@type': 'Thing', payload: `a${String.fromCharCode(0x2028)}b${String.fromCharCode(0x2029)}c` };
    const body = bodyOf(jsonLdScriptTag(node));
    expect(body).not.toContain(String.fromCharCode(0x2028));
    expect(body).not.toContain(String.fromCharCode(0x2029));
    expect(JSON.parse(body)).toEqual(node);
  });

  it('keeps "&" readable (character references are not decoded inside a script element)', () => {
    const body = bodyOf(jsonLdScriptTag({ '@type': 'Organization', name: 'Craft & Ship' }));
    expect(body).toContain('Craft & Ship');
  });

  it('drops characters that could break out of the nonce attribute', () => {
    const tag = jsonLdScriptTag({ '@type': 'Thing' }, 'abc"><script>alert(1)</script>');
    expect(tag.startsWith('<script type="application/ld+json" nonce="abcscriptalert1/script">')).toBe(true);
    const good = jsonLdScriptTag({ '@type': 'Thing' }, 'Zm9v+/8=');
    expect(good).toContain(' nonce="Zm9v+/8=">');
  });

  it('produces valid JSON inside the tag', () => {
    const node = { '@context': 'https://schema.org', '@type': 'Organization', name: 'X' };
    const tag = jsonLdScriptTag(node);
    const inner = tag.replace(/^<script type="application\/ld\+json">/, '').replace(/<\/script>$/, '');
    expect(() => JSON.parse(inner)).not.toThrow();
    expect(JSON.parse(inner)).toEqual(node);
  });
});
