import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  translateExtractedSpecToV1,
  isSafeSqlType,
  MODEL_PLURAL_RE,
  RESERVED_PUBLIC_PATHS,
  RESERVED_TABLE_NAMES,
  RESERVED_MODEL_SLUGS,
  RESERVED_CUSTOM_PAGE_PATHS,
  MAX_ROLE_NAME_LENGTH,
  ARCHETYPE_MODEL_SLUGS,
  ARCHETYPE_SHOWN_TABLES,
  ARCHETYPE_DETAIL_ROUTE_PAGES,
  ARCHETYPE_PAGE_SLUGS,
  copiedPageRoute,
  fieldKind,
  foreignKeyColumn,
  isPersonalStockPhotoName,
  sqlTypeKind,
  publicListingPathProblem,
  engineSlugForModelName,
  isEngineSafeAdminEmail,
  KNOWN_ARCHETYPES,
  normalizeSectionType,
  type ExtractedSpecInput,
  type V1ShapeSpec,
} from '../../src/services/harbor_to_compose';

/**
 * Tests for the harbor → compose translator.
 *
 * Coverage philosophy:
 *   - Per-archetype golden tests prove the translator produces a v1-shape
 *     spec that the v2 worker's auto-detect → translate_v1_spec path
 *     accepts. The shape contract is asserted: required keys present,
 *     no nested pages[].sections, no top-level archetype, _seed_settings
 *     populated, data_models normalized.
 *   - Section-type unit tests cover every alias the translator
 *     understands.
 *   - Edge-case tests cover malformed / missing / oversized inputs.
 *   - Future-cast tests cover features the conversation may surface
 *     before the v2 compose engine renders them natively (booking
 *     calendars, intake forms, payment buttons).
 *
 * The translator MUST be deterministic: same input -> same output.
 * Tests assert that explicitly via deepEqual on repeated calls.
 */

// ============================================================
// Helpers
// ============================================================

function baseInput(overrides: Partial<ExtractedSpecInput> = {}): ExtractedSpecInput {
  return {
    spec_version: '1.0',
    app_name: 'sample-keeper',
    business_type: 'practitioner',
    archetype: 'healer-wellness',
    tier: 'build',
    admin_email: 'sample@example.com',
    pages: [],
    data_models: [],
    copy: {},
    design: {},
    _extraction_warnings: [],
    ...overrides,
  };
}

function expectShapeContract(spec: V1ShapeSpec) {
  // The v1 contract: spec_version + app_name + tier + description.
  expect(spec.spec_version).toBeTruthy();
  expect(spec.app_name).toBeTruthy();
  expect(typeof spec.app_name).toBe('string');
  expect(spec.app_name).toMatch(/^[a-z0-9-]+$/);
  expect(['build', 'grow', 'scale']).toContain(spec.tier);
  expect(spec.description).toBeTruthy();
  // No top-level archetype (worker classifies from description).
  expect((spec as Record<string, unknown>).archetype).toBeUndefined();
  // No nested pages[].sections: the translator hoists everything.
  if (spec.pages) {
    for (const p of spec.pages) {
      expect((p as Record<string, unknown>).sections).toBeUndefined();
      expect(p.path).toMatch(/^\/[a-z0-9-]+/);
    }
  }
}

// ============================================================
// Determinism
// ============================================================

describe('harbor_to_compose: determinism', () => {
  it('same input produces byte-identical output across calls', () => {
    const input = baseInput({
      app_name: 'maya-reiki',
      copy: {
        hero_tagline: 'sitting with someone',
        hero_subtext: 'quiet attention, 75-minute sessions',
        about_body: 'I work with the nervous system. No mysticism.',
      },
      pages: [
        {
          name: 'Home',
          slug: 'home',
          sections: [
            { type: 'hero', content: { tagline: 'sitting with someone', subtext: 'quiet' } },
            { type: 'cta', content: {} },
          ],
        },
      ],
      data_models: [
        { name: 'Booking', fields: [{ name: 'client_name', type: 'string' }] },
      ],
    });
    const a = translateExtractedSpecToV1(input);
    const b = translateExtractedSpecToV1(input);
    expect(JSON.stringify(a)).toBe(JSON.stringify(b));
  });
});

// ============================================================
// Per-archetype golden tests
// ============================================================

describe('harbor_to_compose: per-archetype golden coverage', () => {
  for (const arche of KNOWN_ARCHETYPES) {
    it(`produces a worker-compatible v1-shape spec for archetype: ${arche}`, () => {
      const input = baseInput({
        app_name: `${arche}-keeper`,
        archetype: arche,
        business_type: arche.replace('-', ' ') + ' practice',
        copy: {
          hero_tagline: 'Quiet, grounded work',
          hero_subtext: 'Working with what is actually here.',
          about_body: 'A small practice rooted in real attention.',
        },
        pages: [
          {
            name: 'Home',
            slug: 'home',
            sections: [{ type: 'hero', content: { tagline: 'Quiet, grounded work', subtext: 'Working...' } }],
          },
        ],
      });
      const { spec } = translateExtractedSpecToV1(input);
      expectShapeContract(spec);
      // Worker auto-detect contract: needs at least one of spec_version/
      // user_roles/data_models AND no archetype. spec_version always set.
      expect(spec.spec_version).toBe('1.0');
      // Archetype hint forwarded as metadata (non-load-bearing for worker).
      expect(spec._harbor_meta?.archetype_hint).toBe(arche);
    });
  }
});

// ============================================================
// Section-type unit tests
// ============================================================

describe('harbor_to_compose: section-type aliases', () => {
  it('hero section hoists tagline + subtext into _seed_settings', () => {
    const input = baseInput({
      pages: [{
        name: 'Home', slug: 'home',
        sections: [{
          type: 'hero',
          content: { tagline: 'Quiet attention', subtext: 'For the work that asks for stillness.' },
        }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._seed_settings?.hero_tagline).toBe('Quiet attention');
    expect(spec._seed_settings?.hero_description).toBe('For the work that asks for stillness.');
  });

  it('about section hoists body + heading into _seed_settings', () => {
    const input = baseInput({
      pages: [{
        slug: 'about', name: 'About',
        sections: [{ type: 'about', content: { body: 'A small practice.', heading: 'About me' } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._seed_settings?.about_body).toBe('A small practice.');
    expect(spec._seed_settings?.about_title).toBe('About me');
  });

  it('cta section is silent-handled (no warning produced)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'cta', content: { primary_label: 'Book' } }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.find((w) => /cta/i.test(w))).toBeUndefined();
  });

  it('offerings/services/menu sections collect items into _harbor_meta.services', () => {
    const input = baseInput({
      pages: [
        {
          slug: 'menu', name: 'Menu',
          sections: [{
            type: 'menu',
            content: { items: [{ name: 'Espresso', price: '$4' }, { title: 'Croissant' }] },
          }],
        },
        {
          slug: 'services', name: 'Services',
          sections: [{
            type: 'service_list',
            content: { list: [{ title: '60-min session', description: 'One on one.' }] },
          }],
        },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const titles = spec._harbor_meta?.services?.map((s) => s.title);
    expect(titles).toContain('Espresso');
    expect(titles).toContain('Croissant');
    expect(titles).toContain('60-min session');
  });

  it('dedupes the same service emitted across sections (case-insensitive) + merges price', () => {
    // Opus often emits the same offering in an "offerings" section AND a
    // "/services" page; the public site then renders duplicate cards.
    const input = baseInput({
      pages: [
        {
          slug: 'home', name: 'Home',
          sections: [{
            type: 'offerings',
            content: { items: [{ title: '1:1 Breathwork Session' }, { title: 'Group Circle', price: '$40' }] },
          }],
        },
        {
          slug: 'services', name: 'Services',
          sections: [{
            type: 'service_list',
            content: { list: [
              { title: '1:1 Breathwork Session', price: '$120' }, // dup of home, has price
              { title: 'group circle' },                          // dup (different case)
              { title: 'Retreats' },                              // unique
            ] },
          }],
        },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const services = spec._harbor_meta?.services || [];
    const titles = services.map((s) => s.title);
    // 3 unique services, no duplicates
    expect(titles).toEqual(['1:1 Breathwork Session', 'Group Circle', 'Retreats']);
    // price merged from the later duplicate onto the first (which lacked it)
    expect(services.find((s) => s.title === '1:1 Breathwork Session')?.price).toBe('$120');
  });

  it('testimonials/reviews/press sections collect quotes into _harbor_meta.testimonials', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: 'reviews',
          content: { items: [{ quote: 'Changed my week.', author: 'A.' }, { text: 'Quiet and steady.' }] },
        }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._harbor_meta?.testimonials?.length).toBe(2);
    expect(spec._harbor_meta?.testimonials?.[0].quote).toBe('Changed my week.');
    expect(spec._harbor_meta?.testimonials?.[1].quote).toBe('Quiet and steady.');
  });

  it('contact section hoists email + phone (no email duplication)', () => {
    const input = baseInput({
      pages: [{
        slug: 'contact', name: 'Contact',
        sections: [{ type: 'contact_form', content: { email: 'hello@example.com', phone: '555-0100' } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._seed_settings?.contact_email).toBe('hello@example.com');
    expect(spec._seed_settings?.contact_phone).toBe('555-0100');
  });

  it('visit section (restaurant/cafe) hoists address + phone', () => {
    const input = baseInput({
      archetype: 'restaurant-cafe',
      pages: [{
        slug: 'visit', name: 'Visit',
        sections: [{ type: 'hours', content: { address: '100 Example St, Sample City, ST', phone: '555-555-0199' } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._seed_settings?.contact_address).toBe('100 Example St, Sample City, ST');
    expect(spec._seed_settings?.contact_phone).toBe('555-555-0199');
  });

  it('archetype-handled sections (gallery/newsletter/faq/pricing/team/areas/events/trust) are silent-passed', () => {
    const ARCHETYPE_HANDLED = ['gallery', 'photos', 'newsletter', 'faq', 'pricing', 'team', 'staff', 'areas', 'events', 'past_events', 'trust'];
    for (const t of ARCHETYPE_HANDLED) {
      const input = baseInput({
        pages: [{ slug: 'home', name: 'Home', sections: [{ type: t, content: {} }] }],
      });
      const { warnings } = translateExtractedSpecToV1(input);
      expect(warnings.find((w) => w.includes(`unknown section type "${t}"`))).toBeUndefined();
    }
  });

  it('unknown section types produce a warning + are dropped', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'crystal-tower-3d', content: { foo: 'bar' } }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /crystal-tower-3d/.test(w))).toBe(true);
  });
});

// ============================================================
// Dash-to-underscore alias normalization (sample-site
// regression: Opus emitted "quick-nav", "photo-gallery", etc. and
// every section dropped because the alias map only had "_" keys).
// ============================================================

describe('harbor_to_compose: dash-vs-underscore section type normalization', () => {
  it('dash-separated "photo-gallery" maps to gallery handler (not unknown)', () => {
    const input = baseInput({
      pages: [{
        slug: 'photos', name: 'Photos',
        sections: [{ type: 'photo-gallery', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "photo-gallery"/.test(w))).toBe(false);
  });

  it('dash-separated "contact-form" maps to contact handler (not unknown)', () => {
    const input = baseInput({
      pages: [{
        slug: 'contact', name: 'Contact',
        sections: [{ type: 'contact-form', content: { email: 'hi@example.test' } }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    expect(spec._seed_settings?.contact_email).toBe('hi@example.test');
  });

  it('dash-separated "about-bio" maps to about handler', () => {
    const input = baseInput({
      pages: [{
        slug: 'about', name: 'About',
        sections: [{ type: 'about-bio', content: { body: 'Bio body', heading: 'Bio heading' } }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    expect(spec._seed_settings?.about_body).toBe('Bio body');
    expect(spec._seed_settings?.about_title).toBe('Bio heading');
  });

  it('mixed-case dashed input is normalized (PHOTO-GALLERY, About-Bio)', () => {
    const input = baseInput({
      pages: [{
        slug: 'p', name: 'P',
        sections: [
          { type: 'PHOTO-GALLERY', content: {} },
          { type: 'About-Bio', content: { body: 'B' } },
        ],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
  });
});

// ============================================================
// _preview / _teaser suffix stripping
// ============================================================

describe('harbor_to_compose: _preview / _teaser suffix stripping', () => {
  // Root cause seen on the Fern & Stone end-to-end sample:
  // captured model output for an invented sample included `offerings_preview` + `gallery_preview` on
  // the home page; the translator dropped them as unknown, leaving
  // `_harbor_meta.services` empty so the home offerings widget rendered
  // archetype defaults instead of the sample's own prices.

  it('offerings_preview maps to offerings handler (services list populated)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: 'offerings_preview',
          content: {
            items: [
              { title: 'Single Reiki Session', price: '$120' },
              { title: 'Reiki 3-Pack', price: '$320' },
            ],
          },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "offerings_preview"/.test(w))).toBe(false);
    const services = (spec._harbor_meta as Record<string, unknown>)?.services as unknown[];
    expect(services).toHaveLength(2);
    expect(services[0]).toMatchObject({ title: 'Single Reiki Session', price: '$120' });
    expect(services[1]).toMatchObject({ title: 'Reiki 3-Pack', price: '$320' });
  });

  it('gallery_preview maps to gallery handler (no warning)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'gallery_preview', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "gallery_preview"/.test(w))).toBe(false);
  });

  it('testimonials_preview maps to testimonials handler', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: 'testimonials_preview',
          content: { items: [{ quote: 'Calm and grounded.', author: 'River' }] },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "testimonials_preview"/.test(w))).toBe(false);
    const testimonials = (spec._harbor_meta as Record<string, unknown>)?.testimonials as unknown[];
    expect(testimonials).toHaveLength(1);
  });

  it('about_preview maps to about handler (body/title flow)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: 'about_preview',
          content: { body: 'Trained at ICRT, 8 years.', heading: 'About' },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    expect(spec._seed_settings?.about_body).toBe('Trained at ICRT, 8 years.');
  });

  it('events_preview maps to upcoming_events handler', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'events_preview', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "events_preview"/.test(w))).toBe(false);
  });

  it('dash-separated `offerings-preview` strips to offerings (combined dash + suffix normalization)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: 'offerings-preview',
          content: { items: [{ title: 'Trial Session', price: '$60' }] },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    const services = (spec._harbor_meta as Record<string, unknown>)?.services as unknown[];
    expect(services).toHaveLength(1);
  });

  it('_teaser suffix is also stripped (services_teaser -> offerings)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: 'services_teaser',
          content: { items: [{ title: 'Consult', price: '$80' }] },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    const services = (spec._harbor_meta as Record<string, unknown>)?.services as unknown[];
    expect(services).toHaveLength(1);
  });

  it('normalizeSectionType strips suffix (unit-level)', () => {
    expect(normalizeSectionType('offerings_preview')).toBe('offerings');
    expect(normalizeSectionType('gallery-preview')).toBe('gallery');
    expect(normalizeSectionType('Testimonials_TEASER')).toBe('testimonials');
    // Non-preview suffixes untouched.
    expect(normalizeSectionType('offerings')).toBe('offerings');
    expect(normalizeSectionType('quick-nav')).toBe('quick_nav');
  });
});

describe('harbor_to_compose: fern e2e empirical aliases', () => {
  // From the Fern & Stone end-to-end sample: a captured model call
  // on an invented Reiki/healer conversation emitted these section types
  // that the translator did not recognize. Each is mapped to the closest
  // existing widget.

  it('sessions_list maps to offerings (Fern & Stone sample, captured model output)', () => {
    const input = baseInput({
      pages: [{
        slug: 'sessions', name: 'Sessions',
        sections: [{
          type: 'sessions_list',
          content: { items: [{ title: 'Single', price: '$120' }, { title: '3-Pack', price: '$320' }] },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "sessions_list"/.test(w))).toBe(false);
    const services = (spec._harbor_meta as Record<string, unknown>)?.services as unknown[];
    expect(services).toHaveLength(2);
    expect(services[0]).toMatchObject({ title: 'Single', price: '$120' });
  });

  it('sessions_overview maps to offerings (Fern & Stone sample, captured model output)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: 'sessions_overview',
          content: { items: [{ title: 'Reiki', price: '$120' }] },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    const services = (spec._harbor_meta as Record<string, unknown>)?.services as unknown[];
    expect(services).toHaveLength(1);
  });

  it('sessions (singular plural form) maps to offerings', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'sessions', content: { items: [{ title: 'X', price: '$1' }] } }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    const services = (spec._harbor_meta as Record<string, unknown>)?.services as unknown[];
    expect(services).toHaveLength(1);
  });

  it('gallery-grid (dashed) maps to gallery (Fern & Stone sample, captured model output)', () => {
    const input = baseInput({
      pages: [{
        slug: 'gallery', name: 'Gallery',
        sections: [{ type: 'gallery-grid', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "gallery-grid"/.test(w))).toBe(false);
  });

  it('photo (singular) maps to gallery (Fern & Stone sample, captured model output)', () => {
    const input = baseInput({
      pages: [{
        slug: 'about', name: 'About',
        sections: [{ type: 'photo', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
  });

  it('map maps to visit/location handler (Fern & Stone sample, captured model output)', () => {
    const input = baseInput({
      pages: [{
        slug: 'contact', name: 'Contact',
        sections: [{ type: 'map', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "map"/.test(w))).toBe(false);
  });

  it.each([
    'testimonial',
    'testimonials_carousel',
    'testimonial_section',
    'testimonials_section',
    'client_quotes',
    'quotes',
    'review_list',
  ])('%s alias maps to testimonials handler (no unknown warning)', (sectionType) => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{
          type: sectionType,
          content: { items: [{ quote: 'Great work', author: 'Maya' }] },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type/.test(w))).toBe(false);
    const t = (spec._harbor_meta as Record<string, unknown>)?.testimonials as unknown[];
    expect(Array.isArray(t)).toBe(true);
    expect(t.length).toBeGreaterThanOrEqual(1);
  });

  it('fern e2e #4: nav_paths populated from custom_pages', () => {
    const input = baseInput({
      pages: [
        { slug: 'about', name: 'About', path: '/about',
          sections: [{ type: 'about', content: { body: 'x' } }] },
        { slug: 'sessions', name: 'Sessions', path: '/sessions',
          sections: [{ type: 'sessions_list', content: { items: [] } }] },
        { slug: 'contact', name: 'Contact', path: '/contact',
          sections: [{ type: 'contact_form', content: {} }] },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const navPaths = (spec._harbor_meta as Record<string, unknown>)?.nav_paths as string[] | undefined;
    expect(Array.isArray(navPaths)).toBe(true);
    expect(navPaths!).toEqual(expect.arrayContaining(['/about', '/sessions', '/contact']));
  });

  it('fern e2e #4: nav_paths absent when no custom pages declared', () => {
    const input = baseInput({ pages: [] });
    const { spec } = translateExtractedSpecToV1(input);
    const meta = spec._harbor_meta as Record<string, unknown> | undefined;
    expect(meta?.nav_paths).toBeUndefined();
  });

  it('about_intro and about_section map to about handler', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [
          { type: 'about_intro', content: { body: 'Intro body' } },
          { type: 'about_section', content: { body: 'Section body' } },
        ],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    // First-write wins on duplicate copy fields; either body is acceptable.
    expect(['Intro body', 'Section body']).toContain(spec._seed_settings?.about_body);
  });
});

// ============================================================
// New aliases for harbor section types Opus actually emits
// ============================================================

describe('harbor_to_compose: new aliases (quick_nav, featured_events, event_calendar, photo_gallery, about_bio)', () => {
  it('quick_nav maps to cta (silent-handled)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'quick_nav', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown section type "quick_nav"/.test(w))).toBe(false);
  });

  it('featured_events maps to upcoming_events (silent-handled, archetype default carries the data)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'featured_events', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
  });

  it('event_calendar maps to upcoming_events (silent-handled)', () => {
    const input = baseInput({
      pages: [{
        slug: 'events', name: 'Events',
        sections: [{ type: 'event_calendar', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
  });

  it('event_list maps to upcoming_events (silent-handled): added after sample-site round 3', () => {
    const input = baseInput({
      pages: [{
        slug: 'events', name: 'Events',
        sections: [{ type: 'event_list', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
  });

  it('media_gallery maps to gallery (silent-handled): added after sample-site round 3', () => {
    const input = baseInput({
      pages: [{
        slug: 'media', name: 'Media',
        sections: [{ type: 'media-gallery', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
  });

  it('about_body maps to about (carries body into _seed_settings): added after sample-site round 3', () => {
    const input = baseInput({
      pages: [{
        slug: 'about', name: 'About',
        sections: [{ type: 'about_body', content: { body: 'Body text', heading: 'Hi' } }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /unknown/.test(w))).toBe(false);
    expect(spec._seed_settings?.about_body).toBe('Body text');
  });

  it('event_detail produces a useful drop warning (page-level concept, not section-level)', () => {
    const input = baseInput({
      pages: [{
        slug: 'event', name: 'Event',
        sections: [{ type: 'event-detail', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /event_detail.*page-level/i.test(w))).toBe(true);
    // Must NOT be the generic "unknown section type" warning.
    expect(warnings.some((w) => /unknown section type "event-detail"/.test(w))).toBe(false);
  });

  it('video_gallery produces a useful drop warning (no compose widget)', () => {
    const input = baseInput({
      pages: [{
        slug: 'video', name: 'Video',
        sections: [{ type: 'video-gallery', content: {} }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /video_gallery widget not yet supported/i.test(w))).toBe(true);
    expect(warnings.some((w) => /unknown section type "video-gallery"/.test(w))).toBe(false);
  });
});

// ============================================================
// Empty-stub guard (cross-archetype, root cause of sample-site-v2
// /video build).
// ============================================================

describe('harbor_to_compose: empty-stub custom page guard', () => {
  it('drops custom /video page when only video-gallery section emitted (explained-drop, no form_target)', () => {
    const input = baseInput({
      pages: [{
        slug: 'video', name: 'Video',
        sections: [{ type: 'video-gallery', content: {} }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(spec.pages?.find((p) => p.path === '/video')).toBeUndefined();
    expect(
      warnings.some((w) => /custom page "video" dropped/.test(w))
    ).toBe(true);
    // Useful drop warning for the section itself still fires upstream.
    expect(
      warnings.some((w) => /video_gallery widget not yet supported/i.test(w))
    ).toBe(true);
  });

  it('drops custom page when ALL sections are unknown types (no widget mapping)', () => {
    const input = baseInput({
      pages: [{
        slug: 'lounge', name: 'Lounge',
        sections: [
          { type: 'made-up-thing', content: {} },
          { type: 'glitter-cannon', content: {} },
        ],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(spec.pages?.find((p) => p.path === '/lounge')).toBeUndefined();
    expect(
      warnings.some((w) => /custom page "lounge" dropped/.test(w))
    ).toBe(true);
  });

  it('keeps custom page when at least one section is renderable (e.g. photo-gallery)', () => {
    const input = baseInput({
      pages: [{
        slug: 'studio-photos', name: 'Studio Photos',
        sections: [
          { type: 'photo-gallery', content: {} },
          { type: 'video-gallery', content: {} },
        ],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec.pages?.find((p) => p.path === '/studio-photos')).toBeDefined();
  });

  it('keeps custom page with no sections at all (keeper-defined placeholder)', () => {
    // Mirrors the existing "Studio Tour" / "Press" precedent: a page
    // with no sections is a keeper-intentional placeholder, kept so
    // they can fill it via the admin surface post-launch.
    const input = baseInput({
      pages: [{ slug: 'press', name: 'Press', sections: [] }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec.pages?.find((p) => p.path === '/press')).toBeDefined();
  });

  it('keeps custom page when sections all drop but purpose text is substantive', () => {
    const input = baseInput({
      pages: [{
        slug: 'video', name: 'Video',
        purpose:
          'Video reels of recent live sets with timestamps for festival highlights and studio mixes.',
        sections: [{ type: 'video-gallery', content: {} }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec.pages?.find((p) => p.path === '/video')).toBeDefined();
  });

  it('keeps custom page when form_target resolves even if all sections drop', () => {
    const input = baseInput({
      data_models: [{ name: 'BookingIntent', fields: [{ name: 'requester_name', type: 'string' }] }],
      pages: [{
        slug: 'book', name: 'Book',
        sections: [{ type: 'video-gallery', content: {} }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const bookPage = spec.pages?.find((p) => p.path === '/book');
    expect(bookPage).toBeDefined();
    expect(bookPage?.form_target).toBe('BookingIntent');
  });

  it('cross-archetype: empty-stub guard fires for every archetype', () => {
    for (const archetype of KNOWN_ARCHETYPES) {
      const { spec, warnings } = translateExtractedSpecToV1(
        baseInput({
          archetype,
          pages: [{
            slug: 'video', name: 'Video',
            sections: [{ type: 'video-gallery', content: {} }],
          }],
        })
      );
      expect(
        spec.pages?.find((p) => p.path === '/video'),
        `archetype ${archetype}`
      ).toBeUndefined();
      expect(
        warnings.some((w) => /custom page "video" dropped/.test(w)),
        `archetype ${archetype}`
      ).toBe(true);
    }
  });
});

// ============================================================
// Barren-spec guard (root cause of the sample-site crash)
// ============================================================

describe('harbor_to_compose: barren-spec guard', () => {
  it('emits BARREN_SPEC warning when Opus emitted sections but every one dropped', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [
          { type: 'glitter-cannon', content: {} },
          { type: 'made-up-thing', content: {} },
          { type: 'foo-bar', content: {} },
        ],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /BARREN_SPEC/.test(w))).toBe(true);
  });

  it('does NOT emit BARREN_SPEC when 0 sections were seen (empty pages: archetype defaults are the story)', () => {
    const input = baseInput({
      pages: [{ slug: 'home', name: 'Home', sections: [] }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /BARREN_SPEC/.test(w))).toBe(false);
  });

  it('does NOT emit BARREN_SPEC when at least one section handled', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [
          { type: 'hero', content: { tagline: 'Hi' } },
          { type: 'glitter-cannon', content: {} },
        ],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /BARREN_SPEC/.test(w))).toBe(false);
  });

  it('emits THIN_SPEC warning when many sections seen but most drop (>4 seen, <25% handled)', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [
          { type: 'hero', content: { tagline: 'Hi' } },
          { type: 'foo', content: {} },
          { type: 'bar', content: {} },
          { type: 'baz', content: {} },
          { type: 'qux', content: {} },
          { type: 'quux', content: {} },
        ],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /THIN_SPEC/.test(w))).toBe(true);
  });

  it('regression: sample-site-shape input no longer drops every section silently', () => {
    // This is the ACTUAL spec shape that crashed. Before
    // the fix, all 8 sections dropped and BARREN_SPEC wasn't even
    // detected. After the fix, dash-aware aliases handle most types
    // and BARREN_SPEC fires only when the spec is genuinely unmappable.
    const input = baseInput({
      pages: [
        { slug: 'home', name: 'Home', sections: [{ type: 'quick-nav', content: {} }, { type: 'featured-events', content: {} }] },
        { slug: 'events', name: 'Events', sections: [{ type: 'event-calendar', content: {} }] },
        { slug: 'events/:event-slug', name: 'Event Page', sections: [{ type: 'event-detail', content: {} }] },
        { slug: 'video', name: 'Video', sections: [{ type: 'video-gallery', content: {} }] },
        { slug: 'photos', name: 'Photos', sections: [{ type: 'photo-gallery', content: {} }] },
        { slug: 'contact', name: 'Contact', sections: [{ type: 'contact-form', content: { email: 'sample-site@example.com' } }] },
        { slug: 'about', name: 'About', sections: [{ type: 'about-bio', content: { body: 'DJ bio' } }] },
      ],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    // No BARREN_SPEC: most sections handled now.
    expect(warnings.some((w) => /BARREN_SPEC/.test(w))).toBe(false);
    // about-bio carries the bio body into seed settings.
    expect(spec._seed_settings?.about_body).toBe('DJ bio');
    // contact-form carries email.
    expect(spec._seed_settings?.contact_email).toBe('sample-site@example.com');
    // event-detail + video-gallery emit explained-drop warnings.
    expect(warnings.some((w) => /page-level/i.test(w))).toBe(true);
    expect(warnings.some((w) => /video_gallery widget not yet supported/i.test(w))).toBe(true);
    // No "unknown section type" warnings for the canonical 8.
    const unknownCount = warnings.filter((w) => /unknown section type/.test(w)).length;
    expect(unknownCount).toBe(0);
  });
});

// ============================================================
// Future-cast: booking, intake, payment
// ============================================================

describe('harbor_to_compose: future-cast booking', () => {
  it('booking section creates BookingIntent data_model + records intents', () => {
    const input = baseInput({
      pages: [{
        slug: 'book', name: 'Book',
        sections: [{
          type: 'booking',
          content: {
            paths: [
              { label: 'Free 15-min consult', duration_minutes: 15, price: 0 },
              { label: '75-min session', duration_minutes: 75, price: 120 },
            ],
          },
        }],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(spec.data_models?.find((m) => m.name === 'BookingIntent')).toBeDefined();
    expect(spec._harbor_meta?.booking_intents?.length).toBe(2);
    expect(spec._harbor_meta?.booking_intents?.[0].duration_minutes).toBe(15);
    expect(spec._harbor_meta?.booking_intents?.[1].price).toBe(120);
    expect(warnings.some((w) => /booking section captured as BookingIntent/.test(w))).toBe(true);
  });

  it('booking section without paths still emits BookingIntent + a default-label intent', () => {
    const input = baseInput({
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'schedule', content: {} }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec.data_models?.find((m) => m.name === 'BookingIntent')).toBeDefined();
    expect(spec._harbor_meta?.booking_intents?.length).toBe(1);
    expect(spec._harbor_meta?.booking_intents?.[0].label).toBeTruthy();
  });

  it('does NOT auto-add BookingIntent when keeper already declared their own Booking-like model', () => {
    const input = baseInput({
      data_models: [
        { name: 'Session', fields: [{ name: 'starts_at', type: 'datetime' }] },
      ],
      pages: [{
        slug: 'book', name: 'Book',
        sections: [{ type: 'booking', content: { paths: [{ label: 'session' }] } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    // BookingIntent is added only if keeper doesn't have one already.
    // Opus may produce its own. Check: we DON'T add BookingIntent if
    // any data_model named BookingIntent exists. Here keeper has Session.
    // BookingIntent is added (BookingIntent name itself isn't claimed).
    expect(spec.data_models?.some((m) => m.name === 'BookingIntent')).toBe(true);
    expect(spec.data_models?.some((m) => m.name === 'Session')).toBe(true);
  });

  it('keeps customer-supplied BookingIntent (no duplicate add)', () => {
    const input = baseInput({
      data_models: [
        { name: 'BookingIntent', fields: [{ name: 'foo', type: 'string' }] },
      ],
      pages: [{
        slug: 'book', name: 'Book',
        sections: [{ type: 'booking', content: { paths: [{ label: 'a' }] } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    // Only one BookingIntent.
    const matches = spec.data_models?.filter((m) => m.name === 'BookingIntent') ?? [];
    expect(matches.length).toBe(1);
    // Keeper's fields preserved (foo).
    expect(matches[0].fields?.some((f) => f.name === 'foo')).toBe(true);
  });

  // 2026-04-29 alignment fix: when the keeper's conversation surfaced
  // booking intent but Opus emitted NO custom page for it (only a
  // booking section), the translator auto-injects /book-a-session
  // with form_target=BookingIntent so the public site has a real
  // form to consume the BookingIntent admin tab.
  it('auto-emits /book-a-session when bookingIntents present but no custom booking page declared', () => {
    const input = baseInput({
      // No pages with /book or /book-a-session in slug; just a section
      // on /home that emits the booking intent.
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'booking', content: { paths: [{ label: 'Book a session' }] } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec.data_models?.some((m) => m.name === 'BookingIntent')).toBe(true);
    const bookPage = spec.pages?.find((p) => p.path === '/book-a-session');
    expect(bookPage, 'translator should auto-emit /book-a-session').toBeDefined();
    expect(bookPage?.form_target).toBe('BookingIntent');
    expect(bookPage?.title).toMatch(/Book a session/i);
    expect(bookPage?.submit_text).toBeTruthy();
    expect(bookPage?.success_message).toBeTruthy();
  });

  it('does NOT auto-emit /book-a-session when keeper already has a booking-like custom page', () => {
    // If Opus already produced a /book or /book-a-session page (or any
    // page with form_target=BookingIntent), the auto-emit must not
    // duplicate. Maya's fixture exercises this for /book.
    const input = baseInput({
      pages: [{
        slug: 'book', name: 'Book',  // explicit /book page
        sections: [{ type: 'booking', content: { paths: [{ label: 'session' }] } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const bookPaths = spec.pages?.filter((p) => p.path === '/book' || p.path === '/book-a-session') ?? [];
    // Only the keeper's /book; no auto-emitted /book-a-session in addition.
    expect(bookPaths.length).toBe(1);
    expect(bookPaths[0].path).toBe('/book');
  });

  it('does NOT auto-emit /book-a-session when the spec lacks a BookingIntent data_model entirely', () => {
    // Edge case: bookingIntents extracted but the model list somehow
    // ended up without BookingIntent. Auto-emit should skip rather
    // than emit an orphaned form_target.
    const input = baseInput({
      // No booking section at all.
      pages: [{
        slug: 'home', name: 'Home',
        sections: [{ type: 'hero', content: { headline: 'Hi' } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._harbor_meta?.booking_intents).toBeFalsy();
    expect(spec.pages?.find((p) => p.path === '/book-a-session')).toBeUndefined();
  });

  // hasBookingPage gate: keeper explicitly declared a /book-a-session
  // page (not just /book). Auto-emit must NOT produce a duplicate.
  it('does NOT auto-emit /book-a-session when keeper already declared a /book-a-session page', () => {
    const input = baseInput({
      pages: [{
        slug: 'book-a-session', name: 'Book a session',
        sections: [{ type: 'booking', content: { paths: [{ label: 'session' }] } }],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const bookSessionPaths =
      spec.pages?.filter((p) => p.path === '/book-a-session') ?? [];
    expect(bookSessionPaths.length, 'no duplicate /book-a-session injected').toBe(1);
    // Keeper's declared page wins; the auto-emit branch's hardcoded
    // copy ('Send my request', 'Thank you...') must NOT have leaked
    // onto the keeper's page.
    expect(bookSessionPaths[0].submit_text).not.toBe('Send my request');
  });

  // hasBookingPage gate: keeper declared a page at an unrelated path
  // (e.g. /reserve) but with form_target=BookingIntent. The auto-emit
  // skip must trigger off form_target alone, not just path matching.
  it('does NOT auto-emit /book-a-session when keeper declared a page at any path with form_target=BookingIntent', () => {
    const input = baseInput({
      data_models: [{ name: 'BookingIntent', fields: [{ name: 'requester_name', type: 'string' }] }],
      pages: [
        {
          slug: 'home', name: 'Home',
          sections: [{ type: 'booking', content: { paths: [{ label: 'session' }] } }],
        },
        {
          slug: 'reserve', name: 'Reserve',
          // Substantive purpose so the page survives section-drop pruning.
          purpose:
            'Visitors land here from the email footer to reserve a slot for an upcoming session.',
          form_target: 'BookingIntent',
          sections: [],
        },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const reservePage = spec.pages?.find((p) => p.path === '/reserve');
    expect(reservePage, 'keeper /reserve page with form_target survives').toBeDefined();
    expect(reservePage?.form_target).toBe('BookingIntent');
    // Auto-emit must skip because hasBookingPage matched on form_target.
    expect(spec.pages?.find((p) => p.path === '/book-a-session')).toBeUndefined();
  });
});

describe('harbor_to_compose: future-cast intake form', () => {
  it('intake_form section produces a warning (compose admin wires fields post-launch)', () => {
    const input = baseInput({
      pages: [{
        slug: 'intake', name: 'Intake',
        sections: [{ type: 'intake_form', content: { fields: ['medical', 'goal'] } }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /intake form captured/.test(w))).toBe(true);
  });
});

describe('harbor_to_compose: future-cast payment', () => {
  it('payment/checkout sections are dropped with a warning (CAS Harbor does not process payments)', () => {
    const input = baseInput({
      pages: [{
        slug: 'checkout', name: 'Checkout',
        sections: [{ type: 'payment', content: { provider: 'stripe' } }],
      }],
    });
    const { warnings } = translateExtractedSpecToV1(input);
    expect(warnings.some((w) => /CAS Harbor doesn't process customer payments/.test(w))).toBe(true);
  });
});

// ============================================================
// Edge cases
// ============================================================

describe('harbor_to_compose: edge cases', () => {
  it('throws on null input', () => {
    expect(() => translateExtractedSpecToV1(null as unknown as ExtractedSpecInput)).toThrow(
      /must be an object/
    );
  });

  it('app_name missing -> harbor- prefix + warning', () => {
    const { spec, warnings } = translateExtractedSpecToV1(baseInput({ app_name: '' }));
    expect(spec.app_name).toMatch(/^harbor-/);
    expect(warnings.some((w) => /app_name missing or invalid/.test(w))).toBe(true);
  });

  it('app_name with whitespace + uppercase + special chars -> kebab slug', () => {
    const { spec } = translateExtractedSpecToV1(baseInput({ app_name: 'Maya  Reiki!! Studio' }));
    expect(spec.app_name).toBe('maya-reiki-studio');
  });

  it('app_name caps at 60 chars', () => {
    const long = 'a-very-long-keeper-name-that-goes-on-and-on-and-on-and-on-and-on-and-on';
    const { spec } = translateExtractedSpecToV1(baseInput({ app_name: long }));
    expect(spec.app_name.length).toBeLessThanOrEqual(60);
  });

  it('unknown tier -> defaults to build with a warning', () => {
    const { spec, warnings } = translateExtractedSpecToV1(baseInput({ tier: 'enterprise' as never }));
    expect(spec.tier).toBe('build');
    expect(warnings.some((w) => /unknown tier "enterprise"/.test(w))).toBe(true);
  });

  it('unknown archetype -> dropped from hint with warning', () => {
    const { spec, warnings } = translateExtractedSpecToV1(baseInput({ archetype: 'astrologer' as never }));
    expect(spec._harbor_meta?.archetype_hint).toBeUndefined();
    expect(warnings.some((w) => /unknown archetype "astrologer"/.test(w))).toBe(true);
  });

  it('admin_email is lowercased + trimmed', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({ admin_email: '  Maya@Example.COM  ' })
    );
    expect(spec.admin_email).toBe('maya@example.com');
  });

  it('data_model with missing name is dropped with warning', () => {
    const { spec, warnings } = translateExtractedSpecToV1(
      baseInput({ data_models: [{ fields: [{ name: 'x', type: 'string' }] }] })
    );
    expect(spec.data_models ?? []).toEqual([]);
    expect(warnings.some((w) => /missing or invalid name/.test(w))).toBe(true);
  });

  it('data_model name is PascalCase-normalized', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({ data_models: [{ name: 'session_log' }] })
    );
    expect(spec.data_models?.[0]?.name).toBe('SessionLog');
  });

  it('data_model duplicates are dropped with warning', () => {
    const { spec, warnings } = translateExtractedSpecToV1(
      baseInput({
        data_models: [
          { name: 'Session', fields: [{ name: 'a', type: 'string' }] },
          { name: 'Session', fields: [{ name: 'b', type: 'string' }] },
        ],
      })
    );
    expect(spec.data_models?.length).toBe(1);
    expect(warnings.some((w) => /duplicate/.test(w))).toBe(true);
  });

  it('field with malformed name is dropped with warning', () => {
    const { spec, warnings } = translateExtractedSpecToV1(
      baseInput({
        data_models: [{ name: 'Foo', fields: [{ name: '', type: 'string' }, { name: '!@#', type: 'string' }] }],
      })
    );
    expect(spec.data_models?.[0]?.fields ?? []).toEqual([]);
    expect(warnings.filter((w) => /Foo: field/.test(w)).length).toBeGreaterThan(0);
  });

  it('field name is snake_case-normalized', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({ data_models: [{ name: 'Foo', fields: [{ name: 'Client Name', type: 'string' }] }] })
    );
    expect(spec.data_models?.[0]?.fields?.[0]?.name).toBe('client_name');
  });

  it('user_roles without role name are dropped', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        user_roles: [
          { description: 'no role name' },
          { role: 'manager', description: 'staff' },
          { role: 'manager' }, // duplicate
        ],
      })
    );
    expect(spec.user_roles?.length).toBe(1);
    expect(spec.user_roles?.[0].role).toBe('manager');
  });

  it('custom pages whose slugs collide with archetype defaults are dropped', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        pages: [
          { name: 'Home', slug: 'home', sections: [] },
          { name: 'About', slug: 'about', sections: [] },
          { name: 'Studio Tour', slug: 'studio-tour', sections: [] },
        ],
      })
    );
    expect(spec.pages?.map((p) => p.path)).toEqual(['/studio-tour']);
  });

  it('custom page with form_target referencing a known data_model is preserved', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        data_models: [{ name: 'Inquiry', fields: [{ name: 'msg', type: 'long_text' }] }],
        pages: [
          { name: 'Apply', slug: 'apply', sections: [], form_target: 'Inquiry' },
        ],
      })
    );
    expect(spec.pages?.[0].form_target).toBe('Inquiry');
  });

  it('custom page with form_target referencing UNKNOWN data_model falls back to slug-based auto-wire (or undefined)', () => {
    // `apply` slug auto-wires to ['Application', 'Inquiry', 'ContactInquiry'].
    // None of those exist here so the result is undefined.
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        pages: [{ name: 'Apply', slug: 'apply-z', sections: [], form_target: 'Nonexistent' }],
      })
    );
    expect(spec.pages?.[0].form_target).toBeUndefined();
  });

  it('custom /book page auto-wires form_target to BookingIntent when no model named in pages list', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        pages: [
          // Booking section creates BookingIntent + a /book custom page.
          { name: 'Book', slug: 'book', sections: [{ type: 'booking', content: { paths: [{ label: 'a' }] } }] },
        ],
      })
    );
    const bookPage = spec.pages?.find((p) => p.path === '/book');
    expect(bookPage).toBeDefined();
    expect(bookPage?.form_target).toBe('BookingIntent');
  });

  // Parametric custom_page guard (root cause of sample-site-v2 build
  // failure: keeper-defined /events/:event-slug rendered
  // 404 because the translator emitted it as a custom_page that
  // shadowed the archetype's default `/events/:identifier` detail
  // handler without supplying a view file). The translator MUST drop
  // any parametric page with a warning so the archetype default wins.
  it('custom page with parametric segment (:slug) is dropped with warning', () => {
    const { spec, warnings } = translateExtractedSpecToV1(
      baseInput({
        archetype: 'event-producer',
        data_models: [{ name: 'Event', fields: [{ name: 'title', type: 'string' }] }],
        pages: [
          { name: 'Event Page', slug: 'events/:event-slug', sections: [] },
        ],
      })
    );
    expect(spec.pages?.find((p) => p.path?.includes(':'))).toBeUndefined();
    expect(
      warnings.some((w) => /parametric segment dropped/.test(w))
    ).toBe(true);
  });

  it('parametric paths dropped across archetypes (regression: cross-archetype)', () => {
    // Cross-archetype regression: any keeper-defined parametric path
    // must be dropped at translation time, regardless of archetype.
    for (const archetype of KNOWN_ARCHETYPES) {
      const { spec, warnings } = translateExtractedSpecToV1(
        baseInput({
          archetype,
          pages: [
            { name: 'Detail', slug: 'blog/:post-slug', sections: [] },
            { name: 'Profile', slug: '/users/:id', sections: [] },
          ],
        })
      );
      const parametricKept = (spec.pages ?? []).filter((p) =>
        p.path?.includes(':')
      );
      expect(parametricKept, `archetype ${archetype}`).toEqual([]);
      expect(
        warnings.filter((w) => /parametric segment dropped/.test(w)).length,
        `archetype ${archetype}`
      ).toBeGreaterThanOrEqual(2);
    }
  });

  it('non-parametric pages alongside parametric ones still pass through', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        pages: [
          { name: 'Studio Tour', slug: 'studio-tour', sections: [] },
          { name: 'Event Page', slug: '/events/:event-slug', sections: [] },
          { name: 'Press', slug: 'press', sections: [] },
        ],
      })
    );
    const paths = spec.pages?.map((p) => p.path) ?? [];
    expect(paths).toContain('/studio-tour');
    expect(paths).toContain('/press');
    expect(paths.find((p) => p?.includes(':'))).toBeUndefined();
  });

  it('non-array pages input is silently coerced to empty', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({ pages: 'oops' as never })
    );
    expect(spec.pages).toBeUndefined();
  });

  it('non-array data_models input is silently coerced to empty', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({ data_models: 'oops' as never })
    );
    expect(spec.data_models).toBeUndefined();
  });

  it('preserves _extraction_warnings (capped at 50)', () => {
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        _extraction_warnings: Array(80).fill('warn'),
      })
    );
    expect(spec._extraction_warnings?.length).toBe(50);
  });

  it('description is built from business_type + about_body and capped at ~600 chars', () => {
    const huge = 'lorem '.repeat(200); // ~1200 chars
    const { spec } = translateExtractedSpecToV1(
      baseInput({
        business_type: 'practice',
        copy: { about_body: huge },
      })
    );
    expect(spec.description.length).toBeLessThanOrEqual(600);
    expect(spec.description).toMatch(/practice/);
  });

  it('description falls back to "Harbor keeper site" when nothing usable', () => {
    const { spec } = translateExtractedSpecToV1({});
    expect(spec.description).toBe('Harbor keeper site');
  });

  it('worker auto-detect contract: spec_version present + archetype absent', () => {
    const { spec } = translateExtractedSpecToV1(baseInput());
    expect(spec.spec_version).toBe('1.0');
    expect((spec as Record<string, unknown>).archetype).toBeUndefined();
  });
});

// ============================================================
// Maya end-to-end fixture (the spec extractor produced this on
// 2026-04-27; the translator must accept it without throwing and
// produce a worker-compatible v1-shape spec).
// ============================================================

describe('harbor_to_compose: Maya fixture (captured model output for an invented sample)', () => {
  const MAYA_OPUS_OUTPUT: ExtractedSpecInput = {
    spec_version: '1.0',
    app_name: 'maya-reiki-studio',
    business_type: 'Reiki practice',
    archetype: 'healer-wellness',
    tier: 'build',
    admin_email: 'maya-e2e@example.com',
    copy: {
      hero_tagline: 'Sitting with someone in difficulty',
      hero_subtext:
        'Nervous system work and quiet attention. 75-minute sessions in person and online.',
      about_body:
        'I offer somatic and nervous system work rooted in presence, not mysticism. Each session is 75 minutes of quiet attention: sitting with someone in difficulty, listening deeply, and holding space.',
    },
    pages: [
      {
        name: 'Home',
        slug: 'home',
        sections: [
          {
            type: 'hero',
            content: {
              tagline: 'Sitting with someone in difficulty',
              subtext: 'Nervous system work and quiet attention.',
            },
          },
          {
            type: 'cta',
            content: { primary_label: 'Book a Free Consult', primary_target: '/book' },
          },
        ],
      },
      {
        name: 'About',
        slug: 'about',
        sections: [
          {
            type: 'about',
            content: { body: 'I offer somatic work...', heading: 'How I Work' },
          },
        ],
      },
      {
        name: 'Book a Session',
        slug: 'book',
        sections: [
          {
            type: 'booking',
            content: {
              paths: [
                { label: 'New Clients', booking_type: 'free_consult', duration_minutes: 15, price: 0 },
                { label: 'Returning Clients', booking_type: 'session', duration_minutes: 75, price: 120 },
              ],
            },
          },
        ],
      },
      {
        name: 'Contact',
        slug: 'contact',
        sections: [{ type: 'contact_form', content: { email: 'maya@example.com' } }],
      },
    ],
    data_models: [
      {
        name: 'Booking',
        fields: [
          { name: 'client_name', type: 'string', required: true },
          { name: 'client_email', type: 'email', required: true },
          { name: 'starts_at', type: 'datetime' },
          { name: 'duration_minutes', type: 'integer' },
          { name: 'session_type', type: 'string' },
        ],
      },
      {
        name: 'IntakeResponse',
        fields: [
          { name: 'submitter_name', type: 'string' },
          { name: 'medical_history', type: 'long_text' },
          { name: 'goals', type: 'long_text' },
        ],
      },
    ],
    design: { primary_color: 'cream/oat', mood: 'spare and calm with warmth' },
    _extraction_warnings: [
      'Calendar/booking system provider not specified',
      "Archetype 'healer-wellness' set as best fit",
    ],
  };

  it('translates without throwing + produces worker-compatible spec', () => {
    const { spec, warnings } = translateExtractedSpecToV1(MAYA_OPUS_OUTPUT);
    expectShapeContract(spec);
    // App name + email preserved.
    expect(spec.app_name).toBe('maya-reiki-studio');
    expect(spec.admin_email).toBe('maya-e2e@example.com');
    // Hero copy hoisted from copy block.
    expect(spec._seed_settings?.hero_tagline).toMatch(/Sitting with someone/);
    expect(spec._seed_settings?.about_body).toMatch(/somatic and nervous system/);
    // Booking section produced BookingIntent + intents metadata.
    expect(spec.data_models?.find((m) => m.name === 'BookingIntent')).toBeDefined();
    expect(spec._harbor_meta?.booking_intents?.length).toBe(2);
    // Customer's data_models preserved + name-normalized.
    expect(spec.data_models?.find((m) => m.name === 'Booking')).toBeDefined();
    expect(spec.data_models?.find((m) => m.name === 'IntakeResponse')).toBeDefined();
    // Color mood captured in metadata.
    expect(spec._harbor_meta?.color_mood).toBeTruthy();
    // No nested sections survive.
    if (spec.pages) {
      for (const p of spec.pages) {
        expect((p as Record<string, unknown>).sections).toBeUndefined();
      }
    }
    // Maya's `/book` slug is NOT an archetype default (healer-wellness
    // pages: home, work-with-me, about, journal, events, gallery,
    // contact). So the translator emits it as a custom page +
    // auto-wires form_target to BookingIntent.
    const bookPage = spec.pages?.find((p) => p.path === '/book');
    expect(bookPage).toBeDefined();
    expect(bookPage?.form_target).toBe('BookingIntent');
    // Booking + extraction warnings surface.
    expect(warnings.length).toBeGreaterThan(0);
    expect(warnings.some((w) => /booking/i.test(w))).toBe(true);
  });
});

// ============================================================
// Faithful-copy mode (v2-faithful-site-copy-mode, 2026-05-08)
// ============================================================

describe('translateExtractedSpecToV1: faithful-copy mode', () => {
  it('forwards a valid copy_source_url onto _harbor_meta', () => {
    const input = baseInput({
      copy_source_url: 'https://sample-irrigation.example/',
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._harbor_meta?.copy_source_url).toBe(
      'https://sample-irrigation.example/'
    );
  });

  it('drops a copy_source_url that is not http(s)', () => {
    const input = baseInput({
      copy_source_url: 'javascript:alert(1)',
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._harbor_meta?.copy_source_url).toBeUndefined();
  });

  it('forwards copy_pages with markdown blocks + hero', () => {
    const input = baseInput({
      copy_source_url: 'https://sample-irrigation.example/',
      copy_pages: [
        {
          path: '/',
          title: 'Sample Irrigation Co',
          markdown_blocks: [
            '# Sample Irrigation Co',
            'A sample paragraph of copy.',
            '- Drip systems',
            '- Repairs',
          ],
          hero: {
            heading: 'Sample Irrigation Co',
            body_html: '<p>A sample paragraph of copy.</p>',
            credential_lines: ['Sample credential one', 'Sample credential two'],
            cta_text_me_phone: '555-555-0112',
          },
        },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const pages = spec._harbor_meta?.copy_pages;
    expect(pages).toBeDefined();
    expect(pages).toHaveLength(1);
    expect(pages?.[0].path).toBe('/');
    expect(pages?.[0].title).toBe('Sample Irrigation Co');
    expect(pages?.[0].markdown_blocks.length).toBe(4);
    expect(pages?.[0].hero?.heading).toBe('Sample Irrigation Co');
    expect(pages?.[0].hero?.cta_text_me_phone).toBe('5555550112'); // digits only
    expect(pages?.[0].hero?.credential_lines).toEqual(['Sample credential one', 'Sample credential two']);
  });

  it('preserves em dashes in copy_pages markdown verbatim', () => {
    // brand-voice carve-out: text imported from a source URL is exempt
    // from the no-em-dash rule. Em dashes in customer-authored copy must
    // pass through unchanged.
    const customerCopyWithEmDash =
      'Sample sentence one' + String.fromCharCode(0x2014) + 'sample sentence two';
    const input = baseInput({
      copy_pages: [
        {
          path: '/',
          markdown_blocks: [customerCopyWithEmDash],
        },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._harbor_meta?.copy_pages?.[0].markdown_blocks[0]).toBe(
      customerCopyWithEmDash
    );
  });

  it('drops copy_pages entries with no markdown content + warns', () => {
    const input = baseInput({
      copy_pages: [
        { path: '/', markdown_blocks: [] },
        {
          path: '/about',
          markdown_blocks: ['Real content here.'],
        },
      ],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    const pages = spec._harbor_meta?.copy_pages ?? [];
    expect(pages).toHaveLength(1);
    expect(pages[0].path).toBe('/about');
    expect(warnings.some((w) => /no markdown content/i.test(w))).toBe(true);
  });

  it('drops malformed paths + warns', () => {
    const input = baseInput({
      copy_pages: [
        { path: 'http://x.example/about', markdown_blocks: ['ok'] }, // origin gets stripped
        { path: '/admin?secret=1', markdown_blocks: ['drops query'] },
        { path: '/with spaces', markdown_blocks: ['fails regex'] },
        { path: '/about', markdown_blocks: ['valid'] },
      ],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    const paths = (spec._harbor_meta?.copy_pages ?? []).map((p) => p.path);
    // origin-stripping yields /about (dedup with the explicit /about),
    // query-stripping yields /admin (kept; /admin is a valid path here),
    // /with spaces is dropped.
    expect(paths).toContain('/about');
    expect(paths).toContain('/admin');
    expect(paths).not.toContain('/with spaces');
    expect(warnings.some((w) => /malformed path|duplicate path/i.test(w))).toBe(
      true
    );
  });

  it('caps copy_pages to 12 entries + warns', () => {
    const many = Array.from({ length: 25 }, (_v, i) => ({
      path: `/p${i}`,
      markdown_blocks: ['a'],
    }));
    const { spec, warnings } = translateExtractedSpecToV1(
      baseInput({ copy_pages: many })
    );
    expect(spec._harbor_meta?.copy_pages?.length).toBe(12);
    expect(warnings.some((w) => /truncated/i.test(w))).toBe(true);
  });

  it('rejects unsafe cta_href schemes on hero', () => {
    const input = baseInput({
      copy_pages: [
        {
          path: '/',
          markdown_blocks: ['x'],
          hero: { cta_href: 'javascript:alert(1)' },
        },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(spec._harbor_meta?.copy_pages?.[0].hero?.cta_href).toBeUndefined();
  });

  it('rejects cta_text_me_phone shorter than 7 digits', () => {
    const input = baseInput({
      copy_pages: [
        {
          path: '/',
          markdown_blocks: ['x'],
          hero: { cta_text_me_phone: '12345' },
        },
      ],
    });
    const { spec } = translateExtractedSpecToV1(input);
    expect(
      spec._harbor_meta?.copy_pages?.[0].hero?.cta_text_me_phone
    ).toBeUndefined();
  });

  it('is deterministic for the same copy_pages input', () => {
    const input = baseInput({
      copy_source_url: 'https://example.com/',
      copy_pages: [
        { path: '/', markdown_blocks: ['Hello.'] },
        { path: '/about', markdown_blocks: ['About.'] },
      ],
    });
    const a = translateExtractedSpecToV1(input);
    const b = translateExtractedSpecToV1(input);
    expect(JSON.stringify(a.spec)).toBe(JSON.stringify(b.spec));
  });
});

describe('model-controlled strings never reach generated DDL or routes unchecked (R1-W2 review)', () => {
  function withModel(model: Record<string, unknown>) {
    return translateExtractedSpecToV1(
      baseInput({ data_models: [model] as ExtractedSpecInput['data_models'] })
    );
  }

  it('isSafeSqlType accepts plain column types and rejects anything else', () => {
    const ok = ['TEXT', 'text', 'VARCHAR(255)', 'varchar( 40 )', 'NUMERIC(10,2)', 'INTEGER', 'BOOLEAN', 'TIMESTAMPTZ', 'JSONB', 'UUID', 'TEXT[]', 'double precision'];
    for (const t of ok) expect(isSafeSqlType(t), t).toBe(true);
    const bad = [
      'TEXT); DROP TABLE builds; --',
      'TEXT DEFAULT (pg_sleep(10))',
      'INTEGER REFERENCES customers(id)',
      "TEXT'",
      'VARCHAR(255) NOT NULL; --',
      '',
      'x'.repeat(41),
    ];
    for (const t of bad) expect(isSafeSqlType(t), t).toBe(false);
  });

  it('isSafeSqlType is ASCII only, like the engine pattern it mirrors', () => {
    // The engine compiles _SAFE_SQL_TYPE_RE with re.ASCII and refuses each of
    // these. The translator must drop them too, or it would hand the engine
    // a spec the engine then refuses. Built from code points so that this
    // file holds no look-alike characters.
    const fullWidthTen = String.fromCharCode(0xff11, 0xff10);
    const dotlessI = String.fromCharCode(0x0131);
    const longS = String.fromCharCode(0x017f);
    const noBreakSpace = String.fromCharCode(0x00a0);
    const lookalikes = [
      `VARCHAR(${fullWidthTen})`,
      `${dotlessI}nteger`,
      `j${longS}on`,
      `numeric(10,${noBreakSpace}2)`,
      `numeric(${String.fromCharCode(0x2003)}10)`,
    ];
    for (const t of lookalikes) expect(isSafeSqlType(t), JSON.stringify(t)).toBe(false);
    // Ordinary ASCII spacing is still fine.
    expect(isSafeSqlType('numeric( 10 ,\t2 )')).toBe(true);
  });

  it('keeps a safe sql_type on a field', () => {
    const { spec } = withModel({
      name: 'Session',
      fields: [{ name: 'title', type: 'string', sql_type: 'VARCHAR(255)' }],
    });
    expect(spec.data_models?.[0].fields?.[0].sql_type).toBe('VARCHAR(255)');
  });

  it('drops an injected sql_type, keeps the field, and records a warning', () => {
    const { spec, warnings } = withModel({
      name: 'Session',
      fields: [{ name: 'title', type: 'string', sql_type: 'TEXT); DROP TABLE builds; --' }],
    });
    const field = spec.data_models?.[0].fields?.[0];
    expect(field).toBeDefined();
    expect(field!.name).toBe('title');
    expect(field!.type).toBe('string');
    expect(field!.sql_type).toBeUndefined();
    expect(JSON.stringify(spec.data_models)).not.toContain('DROP TABLE');
    expect(warnings.some((w) => /Session\.title: sql_type dropped/.test(w))).toBe(true);
  });

  it('drops a non-slug plural, a non-name belongs_to and a non-path public_listing_path', () => {
    const { spec, warnings } = withModel({
      name: 'Offering',
      plural: "offerings'; process.exit(1); //",
      belongs_to: 'Parent); DROP',
      public_listing_path: "/offerings'+require('child_process')+'",
    });
    const model = spec.data_models?.[0];
    expect(model?.plural).toBeUndefined();
    expect(model?.belongs_to).toBeUndefined();
    expect(model?.public_listing_path).toBeUndefined();
    expect(warnings.filter((w) => /dropped \(not a plain/.test(w))).toHaveLength(3);
  });

  it('keeps well-formed plural, belongs_to and public_listing_path', () => {
    const { spec } = withModel({
      name: 'Offering',
      plural: 'Offerings',
      belongs_to: 'Practitioner',
      public_listing_path: '/our-offerings',
    });
    const model = spec.data_models?.[0];
    expect(model?.plural).toBe('offerings');
    expect(model?.belongs_to).toBe('Practitioner');
    expect(model?.public_listing_path).toBe('/our-offerings');
  });
});


// ---------------------------------------------------------------------------
// The translator agrees with the build engine's identifier gate
// ---------------------------------------------------------------------------
//
// compose/build.py refuses (it does not repair) a spec whose model slugs,
// listing paths, app name or admin email do not fit its rules. The translator
// therefore has to drop exactly those values first, or a build fails where it
// should have degraded.

describe('translator: values the build engine would refuse are dropped, with a warning', () => {
  function withModels(models: Array<Record<string, unknown>>, overrides: Partial<ExtractedSpecInput> = {}) {
    return translateExtractedSpecToV1(
      baseInput({ data_models: models as ExtractedSpecInput['data_models'], ...overrides })
    );
  }
  const modelNamed = (spec: V1ShapeSpec, name: string) => spec.data_models?.find((m) => m.name === name);

  describe('plural', () => {
    it.each([
      ['a leading digit', '3d-models'],
      ['an underscore', 'blog_posts'],
      ['a leading hyphen', '-posts'],
      ['a dot', 'blog.posts'],
      ['a slash', 'blog/posts'],
      ['whitespace inside', 'blog posts'],
      ['more than 60 characters', 'p' + 'o'.repeat(60)],
      ['code', "posts'); process.exit(1); //"],
    ])('drops a plural with %s', (_label, plural) => {
      const { spec, warnings } = withModels([{ name: 'Post', plural }]);
      const model = modelNamed(spec, 'Post');
      expect(model).toBeDefined();
      expect(model!.plural).toBeUndefined();
      expect(warnings.some((w) => /data_model "Post": plural dropped \(not a plain slug\)/.test(w))).toBe(true);
    });

    it('keeps a lower-case hyphenated slug (and lower-cases a capitalised one)', () => {
      expect(modelNamed(withModels([{ name: 'Post', plural: 'field-notes' }]).spec, 'Post')!.plural).toBe('field-notes');
      expect(modelNamed(withModels([{ name: 'Post', plural: 'Field-Notes2' }]).spec, 'Post')!.plural).toBe('field-notes2');
      expect(MODEL_PLURAL_RE.test('a')).toBe(true);
      expect(MODEL_PLURAL_RE.test('a' + 'b'.repeat(59))).toBe(true);
      expect(MODEL_PLURAL_RE.test('a' + 'b'.repeat(60))).toBe(false);
    });
  });

  describe('public_listing_path', () => {
    it.each([
      ['the site root', '/', /not a plain absolute path/],
      ['a protocol-relative form', '//x', /not a plain absolute path/],
      ['an empty segment', '/a//b', /not a plain absolute path/],
      ['two trailing slashes', '/offerings//', /not a plain absolute path/],
      ['no leading slash', 'offerings', /not a plain absolute path/],
      ['a dot segment', '/../etc', /not a plain absolute path/],
      ['a file-like segment', '/offerings.html', /not a plain absolute path/],
      ['an Express parameter', '/offerings/:id', /not a plain absolute path/],
      ['an Express wildcard', '/offerings/*', /not a plain absolute path/],
      ['a space', '/our offerings', /not a plain absolute path/],
      ['a quote', "/offerings'+x+'", /not a plain absolute path/],
      ['a segment starting with a hyphen', '/-offerings', /not a plain absolute path/],
      ['nine segments', '/a/b/c/d/e/f/g/h/i', /not a plain absolute path/],
      ['more than 120 characters', '/' + 'a'.repeat(120), /not a plain absolute path/],
      ['/admin', '/admin', /under \/admin/],
      ['/admin with a trailing slash', '/admin/', /under \/admin/],
      ['a path under /admin', '/admin/offerings', /under \/admin/],
      ['/admin in another letter case', '/Admin/offerings', /under \/admin/],
      ['/ADMIN', '/ADMIN', /under \/admin/],
      ['a built-in route', '/login', /collides with a built-in route/],
      ['a built-in route in another case, with a trailing slash', '/Contact/', /collides with a built-in route/],
      ['the uploads mount', '/uploads', /collides with a built-in route/],
      ['the health check', '/health', /collides with a built-in route/],
    ])('drops %s', (_label, listingPath, reason) => {
      expect(publicListingPathProblem(listingPath)).toMatch(reason);
      const { spec, warnings } = withModels([{ name: 'Offering', public_listing_path: listingPath }]);
      const model = modelNamed(spec, 'Offering');
      expect(model).toBeDefined();
      expect(model!.public_listing_path).toBeUndefined();
      expect(warnings.some((w) => /data_model "Offering": public_listing_path dropped/.test(w))).toBe(true);
      expect(JSON.stringify(spec.data_models)).not.toContain(JSON.stringify(listingPath).slice(1, -1));
    });

    it.each(['/offerings', '/our-offerings', '/Offerings', '/work/with_me', '/offerings/', '/a/b/c/d/e/f/g/h', '/administrators', '/logins', '/uploads/gallery'])(
      'keeps %s',
      (listingPath) => {
        expect(publicListingPathProblem(listingPath)).toBeNull();
        const { spec } = withModels([{ name: 'Offering', public_listing_path: listingPath }]);
        expect(modelNamed(spec, 'Offering')!.public_listing_path).toBe(listingPath);
      }
    );

    it('keeps an explicit null (no public listing) and ignores an empty string', () => {
      expect(modelNamed(withModels([{ name: 'Offering', public_listing_path: null }]).spec, 'Offering')!.public_listing_path).toBeNull();
      const empty = withModels([{ name: 'Offering', public_listing_path: '' }]);
      expect(modelNamed(empty.spec, 'Offering')!.public_listing_path).toBeUndefined();
      expect(empty.warnings.some((w) => /public_listing_path dropped/.test(w))).toBe(false);
    });

    it('refuses non-strings', () => {
      for (const value of [null, undefined, 7, {}, ['/x']]) {
        expect(publicListingPathProblem(value)).toMatch(/not a plain absolute path/);
      }
    });
  });

  describe('models whose slug or table the engine owns', () => {
    it('derives the slug the way the engine does', () => {
      expect(engineSlugForModelName('Post')).toBe('posts');
      expect(engineSlugForModelName('BlogPost')).toBe('blog-posts');
      expect(engineSlugForModelName('Category')).toBe('categories');
      expect(engineSlugForModelName('Day')).toBe('days');
      expect(engineSlugForModelName('Class')).toBe('classes');
      expect(engineSlugForModelName('FAQ')).toBe('faqs');
      expect(engineSlugForModelName('Y')).toBe('ys');
    });

    it.each([
      ['User', /reserved table "users"/],
      ['UserSession', /reserved table "user_sessions"/],
      ['SiteSetting', /reserved table "site_settings"/],
      ['Page', /reserved route slug "pages"/],
      ['Image', /reserved route slug "images"/],
      ['Setting', /reserved route slug "settings"/],
      ['Upload', /reserved route slug "uploads"/],
      ['Font', /reserved route slug "fonts"/],
    ])('drops a model named %s', (name, reason) => {
      const { spec, warnings } = withModels([{ name }, { name: 'Offering' }]);
      expect(modelNamed(spec, name)).toBeUndefined();
      expect(modelNamed(spec, 'Offering')).toBeDefined();
      expect(warnings.some((w) => new RegExp(`data_model "${name}" dropped: `).test(w) && reason.test(w))).toBe(true);
    });

    it('a reserved explicit plural is dropped and the model survives on its derived slug', () => {
      const { spec, warnings } = withModels([{ name: 'Member', plural: 'users' }]);
      const model = modelNamed(spec, 'Member');
      expect(model).toBeDefined();
      expect(model!.plural).toBeUndefined();
      expect(warnings.some((w) => /data_model "Member": plural dropped \(maps to the reserved table "users"\)/.test(w))).toBe(true);
    });

    it('two models that would share a table: the later one is dropped', () => {
      const { spec, warnings } = withModels([
        { name: 'Post', plural: 'articles' },
        { name: 'Article' },
        { name: 'Offering' },
      ]);
      expect(modelNamed(spec, 'Post')!.plural).toBe('articles');
      expect(modelNamed(spec, 'Article')).toBeUndefined();
      expect(modelNamed(spec, 'Offering')).toBeDefined();
      expect(warnings.some((w) => /data_model "Article" dropped: maps to the same table as "Post" \("articles"\)/.test(w))).toBe(true);
    });

    it('an explicit plural that collides with an earlier model falls back to the derived slug', () => {
      const { spec } = withModels([{ name: 'Article' }, { name: 'Essay', plural: 'articles' }]);
      expect(modelNamed(spec, 'Article')).toBeDefined();
      const essay = modelNamed(spec, 'Essay');
      expect(essay).toBeDefined();
      expect(essay!.plural).toBeUndefined();
    });

    it('hyphen and underscore forms of one table are recognised as the same table', () => {
      // "field-notes" and FieldNote both live in table field_notes.
      const { spec } = withModels([{ name: 'Entry', plural: 'field-notes' }, { name: 'FieldNote' }]);
      expect(modelNamed(spec, 'Entry')).toBeDefined();
      expect(modelNamed(spec, 'FieldNote')).toBeUndefined();
    });

    it.each([
      ['Post', 'blog-posts', 'BlogPost', 'blog_posts'],
      ['Offering', 'services', 'Service', 'services'],
      ['Praise', 'testimonials', 'Testimonial', 'testimonials'],
      ['Helper', 'staff', 'StaffMember', 'staff'],
    ])('%s may not take the table of an archetype model through plural "%s"', (name, plural, owner, table) => {
      // The engine merges a spec's models into the archetype's by name, then
      // refuses the spec when two different models share a table.
      const { spec, warnings } = withModels([{ name, plural }]);
      const model = modelNamed(spec, name);
      expect(model).toBeDefined();
      expect(model!.plural).toBeUndefined();
      expect(warnings).toContain(
        `data_model "${name}": plural dropped (maps to the table of the archetype model "${owner}" ("${table}"))`
      );
    });

    it('a model of the same name as the archetype model keeps that table (it takes the default\'s place)', () => {
      const { spec, warnings } = withModels([{ name: 'BlogPost', plural: 'blog-posts' }, { name: 'faq' }]);
      expect(modelNamed(spec, 'BlogPost')!.plural).toBe('blog-posts');
      // "faq" becomes "Faq", whose table "faqs" belongs to the archetype model "FAQ": the same name in another letter case.
      expect(modelNamed(spec, 'Faq')).toBeDefined();
      expect(warnings.some((w) => /archetype model/.test(w))).toBe(false);
    });

    it.each(['ContactInquiry', 'NewsletterSubscriber', 'MailingListSubscriber'])(
      '%s is never listed publicly: the contact form or the newsletter signup stores into it',
      (name) => {
        const { spec, warnings } = withModels([{ name, public_listing_path: '/guestbook', fields: [{ name: 'email', type: 'email' }] }]);
        expect(modelNamed(spec, name)).toBeDefined();
        expect(modelNamed(spec, name)!.public_listing_path).toBeUndefined();
        expect(warnings).toContain(
          `data_model "${name}": public_listing_path dropped (the contact form or the newsletter signup stores into this model)`
        );
      }
    );
  });

  describe('custom pages', () => {
    const TITLE = { name: 'title', type: 'string', required: true };
    function withPages(
      pages: Array<Record<string, unknown>>,
      models: Array<Record<string, unknown>> = [],
      overrides: Partial<ExtractedSpecInput> = {}
    ) {
      return withModels(models, { pages: pages as ExtractedSpecInput['pages'], ...overrides });
    }
    const pageAt = (spec: V1ShapeSpec, pagePath: string) => spec.pages?.find((p) => p.path === pagePath);

    describe('allowed_roles', () => {
      it('drops entries the engine cannot check and keeps the rest, each once', () => {
        const { spec, warnings } = withPages([
          { slug: 'members', auth_required: true, allowed_roles: [' member ', '', 'member', '   ', 7, 'm'.repeat(MAX_ROLE_NAME_LENGTH + 1), 'm'.repeat(MAX_ROLE_NAME_LENGTH)] },
        ]);
        expect(pageAt(spec, '/members')!.allowed_roles).toEqual(['member', 'm'.repeat(MAX_ROLE_NAME_LENGTH)]);
        expect(warnings).toContain('custom page "members": 4 allowed_roles entries dropped (not a usable role name)');
      });

      it('a protected page left with no usable role is limited to the admin, never opened to every account', () => {
        const { spec, warnings } = withPages([{ slug: 'members', auth_required: true, allowed_roles: ['m'.repeat(60)] }]);
        expect(pageAt(spec, '/members')!.auth_required).toBe(true);
        expect(pageAt(spec, '/members')!.allowed_roles).toEqual(['admin']);
        expect(warnings).toContain(
          'custom page "members": none of its allowed_roles is a usable role name; the page is limited to the admin'
        );
      });

      it('a public page keeps no unusable entry either, and a clean list passes untouched', () => {
        const blank = withPages([{ slug: 'team-notes', allowed_roles: ['  '] }]);
        expect(pageAt(blank.spec, '/team-notes')!.allowed_roles).toEqual([]);
        expect(blank.warnings).toContain('custom page "team-notes": 1 allowed_roles entry dropped (not a usable role name)');
        const clean = withPages([{ slug: 'members', auth_required: true, allowed_roles: ['member', 'coach'] }]);
        expect(pageAt(clean.spec, '/members')!.allowed_roles).toEqual(['member', 'coach']);
        expect(clean.warnings.some((w) => /allowed_roles/.test(w))).toBe(false);
      });
    });

    describe('paths something else answers', () => {
      it.each(['login', 'privacy', 'terms', 'newsletter-signup', 'magic/consume', 'Admin', 'health'])(
        'a page at the built-in route /%s is dropped, protected or not',
        (slug) => {
          for (const authRequired of [true, false]) {
            const { spec, warnings } = withPages([{ slug, auth_required: authRequired }, { slug: 'notes' }]);
            expect((spec.pages ?? []).map((p) => p.path)).toEqual(['/notes']);
            expect(warnings).toContain(
              `custom page "/${slug.toLowerCase()}" dropped: the site's built-in route answers at that path`
            );
          }
        }
      );

      it('a protected page at a path an archetype serves publicly is dropped, and the warning says so', () => {
        const legacy = withPages([{ slug: 'about', auth_required: true }]);
        expect(legacy.spec.pages).toBeUndefined();
        expect(legacy.warnings).toContain(
          'custom page "about" is marked auth_required but was dropped: the archetype\'s own public page owns that path'
        );
        // /books is a page of the author archetype only; a public page there
        // is still emitted for the other archetypes.
        const books = withPages([{ slug: 'books', auth_required: true }]);
        expect(books.spec.pages).toBeUndefined();
        expect(books.warnings).toContain(
          'custom page "/books" is marked auth_required but was dropped: an archetype\'s public page would answer at that path without a login'
        );
        expect(pageAt(withPages([{ slug: 'books' }]).spec, '/books')).toBeDefined();
      });

      it('a protected page one level below an archetype listing with per-row pages is dropped', () => {
        const { spec, warnings } = withPages([
          { slug: 'journal/archive', auth_required: true },
          { slug: 'events/vip', auth_required: true },
          { slug: 'journal/notes' },
          { slug: 'journal/archive/2024', auth_required: true },
        ]);
        expect((spec.pages ?? []).map((p) => p.path)).toEqual(['/journal/notes', '/journal/archive/2024']);
        expect(warnings).toContain(
          'custom page "/journal/archive" is marked auth_required but was dropped: the public per-row pages of the archetype\'s /journal listing would answer at that path without a login'
        );
        expect(warnings.some((w) => w.startsWith('custom page "/events/vip" is marked auth_required but was dropped'))).toBe(true);
      });

      it('a protected page exactly at a model listing: the listing is withdrawn and the page stays protected', () => {
        const { spec, warnings } = withPages(
          [{ slug: 'stories', auth_required: true }],
          [{ name: 'Story', public_listing_path: '/Stories/', fields: [TITLE] }]
        );
        expect(pageAt(spec, '/stories')!.auth_required).toBe(true);
        expect(modelNamed(spec, 'Story')!.public_listing_path).toBeNull();
        expect(warnings).toContain(
          'data_model "Story": public_listing_path dropped (the custom page "/stories" at that path is auth_required)'
        );
      });

      it('a protected page below a model listing is dropped and the listing stays', () => {
        const { spec, warnings } = withPages(
          [{ slug: 'stories/members', auth_required: true }, { slug: 'stories/about-these' }],
          [{ name: 'Story', public_listing_path: '/stories', fields: [TITLE] }]
        );
        expect((spec.pages ?? []).map((p) => p.path)).toEqual(['/stories/about-these']);
        expect(modelNamed(spec, 'Story')!.public_listing_path).toBe('/stories');
        expect(warnings).toContain(
          'custom page "/stories/members" is marked auth_required but was dropped: the public per-row pages of the model listed at /stories would answer at that path without a login'
        );
      });
    });

    describe('one page per path', () => {
      it('a path declared public and then protected is emitted once, protected, with the protected roles', () => {
        const { spec, warnings } = withPages([
          { slug: 'vault', title: 'Vault' },
          { path: '/Vault/', auth_required: true, allowed_roles: ['member'] },
        ]);
        expect(spec.pages).toHaveLength(1);
        expect(pageAt(spec, '/vault')).toMatchObject({ title: 'Vault', auth_required: true, allowed_roles: ['member'] });
        expect(warnings).toContain(
          'custom page "/vault" is declared more than once and one declaration is auth_required; the page was emitted once and requires a login'
        );
      });

      it('a protected page is never loosened by a later public declaration', () => {
        const { spec, warnings } = withPages([
          { slug: 'vault', auth_required: true, allowed_roles: ['member'] },
          { slug: 'vault' },
        ]);
        expect(spec.pages).toHaveLength(1);
        expect(pageAt(spec, '/vault')).toMatchObject({ auth_required: true, allowed_roles: ['member'] });
        expect(warnings).toContain('custom page "/vault" is declared more than once; the later declaration was dropped');
      });
    });

    describe('forms that add rows to a model visitors can read', () => {
      it('the form the translator picks for /book is never one into a publicly listed model', () => {
        const listed = withPages([{ slug: 'book', title: 'Book' }], [{ name: 'Session', public_listing_path: '/sessions', fields: [TITLE] }]);
        expect(pageAt(listed.spec, '/book')!.form_target).toBeUndefined();
        expect(modelNamed(listed.spec, 'Session')!.public_listing_path).toBe('/sessions');
        expect(listed.warnings).toContain(
          'custom page "book": no form was attached (the model "Session" it would store into is shown to visitors)'
        );
        // The next candidate that is private is taken instead.
        const two = withPages(
          [{ slug: 'book' }],
          [{ name: 'Session', public_listing_path: '/sessions', fields: [TITLE] }, { name: 'Appointment', fields: [TITLE] }]
        );
        expect(pageAt(two.spec, '/book')!.form_target).toBe('Appointment');
        // A private Session is still picked, as before.
        const unlisted = withPages([{ slug: 'book' }], [{ name: 'Session', fields: [TITLE] }]);
        expect(pageAt(unlisted.spec, '/book')!.form_target).toBe('Session');
      });

      it.each([
        ['a model the spec lists', { name: 'Story', public_listing_path: '/stories', fields: [TITLE] }],
        ['a model an archetype page shows', { name: 'Testimonial', fields: [{ name: 'quote', type: 'text' }] }],
        ['a model on a table an archetype page shows', { name: 'Review', fields: [TITLE] }],
      ])('an explicit form into %s: the model gets a "published" field to hold submissions back', (_label, model) => {
        const { spec, warnings } = withPages([{ slug: 'tell', form_target: model.name }], [model]);
        expect(pageAt(spec, '/tell')!.form_target).toBe(model.name);
        const fields = modelNamed(spec, model.name)!.fields!;
        expect(fields[fields.length - 1]).toEqual({ name: 'published', type: 'boolean', default: true });
        expect(fields.filter((f) => f.name === 'published')).toHaveLength(1);
        expect(warnings.some((w) => w.startsWith(`data_model "${model.name}": a "published" field was added`))).toBe(true);
      });

      it('a model that already has a boolean "published" field is left as it is', () => {
        for (const flag of [{ name: 'Published', type: 'Boolean' }, { name: 'published', sql_type: 'BOOLEAN' }, { name: 'published', type: 'bool' }]) {
          const { spec, warnings } = withPages(
            [{ slug: 'tell', form_target: 'Story' }],
            [{ name: 'Story', public_listing_path: '/stories', fields: [TITLE, flag] }]
          );
          expect(pageAt(spec, '/tell')!.form_target).toBe('Story');
          expect(modelNamed(spec, 'Story')!.fields).toHaveLength(2);
          expect(warnings.some((w) => /published/.test(w))).toBe(false);
        }
      });

      it.each([
        ['a date', { name: 'published', type: 'date' }],
        ['untyped (a TEXT column)', { name: 'published' }],
        ['a boolean type overridden by a text sql_type', { name: 'published', type: 'boolean', sql_type: 'TEXT' }],
      ])('a "published" field that is %s cannot hold a row back: the page loses its form', (_label, flag) => {
        const { spec, warnings } = withPages(
          [{ slug: 'tell', form_target: 'Story' }],
          [{ name: 'Story', public_listing_path: '/stories', fields: [TITLE, flag] }]
        );
        expect(pageAt(spec, '/tell')).toBeDefined();
        expect(pageAt(spec, '/tell')!.form_target).toBeUndefined();
        expect(modelNamed(spec, 'Story')!.fields).toHaveLength(2);
        expect(warnings).toContain(
          'custom page "/tell": form_target "Story" dropped (the model is shown to visitors and its "published" field is not a yes/no field, so a submitted row could not be held back)'
        );
      });

      it('a shown model without a field list gets no form', () => {
        const { spec, warnings } = withPages([{ slug: 'tell', form_target: 'Testimonial' }], [{ name: 'Testimonial' }]);
        expect(pageAt(spec, '/tell')!.form_target).toBeUndefined();
        expect(modelNamed(spec, 'Testimonial')!.fields).toBeUndefined();
        expect(warnings.some((w) => /form_target "Testimonial" dropped \(the model is shown to visitors and it has no field list of its own/.test(w))).toBe(true);
      });

      it('a form into a private model, and a form behind a login, are left alone', () => {
        const privateModel = withPages([{ slug: 'tell', form_target: 'Note' }], [{ name: 'Note', fields: [TITLE] }]);
        expect(pageAt(privateModel.spec, '/tell')!.form_target).toBe('Note');
        expect(modelNamed(privateModel.spec, 'Note')!.fields).toEqual([TITLE]);
        const protectedForm = withPages(
          [{ slug: 'tell', form_target: 'Story', auth_required: true }],
          [{ name: 'Story', public_listing_path: '/stories', fields: [TITLE] }]
        );
        expect(pageAt(protectedForm.spec, '/tell')!.form_target).toBe('Story');
        expect(modelNamed(protectedForm.spec, 'Story')!.fields).toEqual([TITLE]);
      });
    });

    describe('pages copied from an existing site', () => {
      const COPIED = 'A paragraph copied from the existing site, long enough to be kept.';

      it('copiedPageRoute gives the route the engine serves a copied page at', () => {
        expect(copiedPageRoute('/')).toBe('/');
        expect(copiedPageRoute('/home')).toBe('/');
        expect(copiedPageRoute('/about')).toBe('/about');
        expect(copiedPageRoute('/services/coaching')).toBe('/services-coaching');
        expect(copiedPageRoute('/_members_/')).toBe('/members');
        expect(copiedPageRoute('/-/')).toBe('/page');
        expect(copiedPageRoute('/' + 'a'.repeat(63) + '-b')).toBe('/' + 'a'.repeat(63));
      });

      it('a copied page at the route of a protected page is left out; the page stays protected', () => {
        const { spec, warnings } = withPages(
          [{ slug: 'members-area', auth_required: true }, { slug: 'vault', auth_required: true }, { slug: 'notes' }],
          [],
          {
            copy_pages: [
              { path: '/members/area', markdown_blocks: [COPIED] },
              { path: '/vault', markdown_blocks: [COPIED] },
              { path: '/notes', markdown_blocks: [COPIED] },
              { path: '/story', markdown_blocks: [COPIED] },
            ],
          }
        );
        expect((spec._harbor_meta?.copy_pages ?? []).map((p) => p.path)).toEqual(['/notes', '/story']);
        expect(pageAt(spec, '/members-area')!.auth_required).toBe(true);
        expect(pageAt(spec, '/vault')!.auth_required).toBe(true);
        const expected =
          'copy_pages "/members/area" dropped: the custom page at "/members-area" is auth_required, and the copied page would answer there without a login';
        expect(warnings).toContain(expected);
        expect(spec._translation_warnings).toContain(expected);
      });

      it('a warning about a copied page reaches the spec even when it is the only warning', () => {
        const { spec, warnings } = withPages([], [], { copy_pages: [{ path: '/empty', markdown_blocks: [] }] });
        expect(warnings).toEqual(['copy_pages "/empty" dropped: no markdown content captured']);
        expect(spec._translation_warnings).toEqual(warnings);
      });
    });
  });

  describe('app_name and admin_email', () => {
    it('an app name that would start with a digit is prefixed so it starts with a letter', () => {
      const { spec } = withModels([], { app_name: '9 Sample Studio' });
      expect(spec.app_name).toBe('site-9-sample-studio');
      expect(spec.app_name).toMatch(/^[a-z][a-z0-9-]{0,62}$/);
      expect(withModels([], { app_name: 'Sample Studio' }).spec.app_name).toBe('sample-studio');
    });

    it('an app name with the platform\'s reserved prefix is prefixed so the engine builds it', () => {
      // The app name becomes the Railway project name, and the platform
      // refuses to delete a project whose name starts with "craftandship".
      expect(withModels([], { app_name: 'CraftAndShip Studio' }).spec.app_name).toBe('site-craftandship-studio');
      expect(withModels([], { app_name: 'craftandship' }).spec.app_name).toBe('site-craftandship');
      expect(withModels([], { app_name: '--Craftandship--Web' }).spec.app_name).toBe('site-craftandship-web');
      expect(withModels([], { app_name: 'My Craftandship Fan Site' }).spec.app_name).toBe('my-craftandship-fan-site');
    });

    it('keeps a plain admin email and drops anything else', () => {
      expect(withModels([], { admin_email: ' Maya@Example.com ' }).spec.admin_email).toBe('maya@example.com');
      for (const hostile of ['ask the keeper', 'maya@localhost', 'maya@example.com\nbcc: x@y.example', "maya'@example.com", 'a@b.c d']) {
        const { spec, warnings } = withModels([], { admin_email: hostile });
        expect(spec.admin_email, hostile).toBeUndefined();
        expect(warnings).toContain('admin_email dropped (not a plain email address)');
      }
      expect(isEngineSafeAdminEmail(`${'a'.repeat(64)}@${'b'.repeat(63)}.${'c'.repeat(63)}.${'d'.repeat(63)}`)).toBe(false); // over 254
    });
  });
});

// The rules above are copies of constants in compose/build.py. This reads the
// engine's source and compares, so a change on either side that is not made on
// the other fails here. Skipped when the engine is not checked out next to
// web/ (a web-only container).
const ENGINE_SOURCE_PATH = path.join(__dirname, '..', '..', '..', 'compose', 'build.py');
const engineAvailable = fs.existsSync(ENGINE_SOURCE_PATH);

describe.skipIf(!engineAvailable)('translator rules match compose/build.py', () => {
  const source = engineAvailable ? fs.readFileSync(ENGINE_SOURCE_PATH, 'utf-8') : '';

  /** The string members of `NAME = frozenset({ ... })`. */
  function pythonSet(name: string): string[] {
    const block = source.match(new RegExp(`^${name}\\s*=\\s*frozenset\\(\\{([\\s\\S]*?)\\}\\)`, 'm'));
    if (!block) throw new Error(`could not find ${name} in compose/build.py`);
    return [...block[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]).sort();
  }

  /**
   * The pattern of `NAME = re.compile(r"...", re.ASCII)`, including adjacent
   * string pieces, as a full-match RegExp. The engine compiles its
   * allowlists with re.ASCII; a JavaScript RegExp without the `u` and `i`
   * flags matches these explicit character classes the same way.
   */
  function pythonRegex(name: string): RegExp {
    const block = source.match(new RegExp(
      `^${name}\\s*=\\s*re\\.compile\\(\\s*((?:r"[^"\\n]*"\\s*)+)(?:,\\s*re\\.ASCII\\s*,?\\s*)?\\)`, 'm',
    ));
    if (!block) throw new Error(`could not find ${name} in compose/build.py`);
    const pattern = [...block[1].matchAll(/r"([^"\n]*)"/g)].map((m) => m[1]).join('');
    return new RegExp(`^(?:${pattern})$`);
  }

  it('reserved sets are identical', () => {
    expect([...RESERVED_PUBLIC_PATHS].sort()).toEqual(pythonSet('_RESERVED_PUBLIC_PATHS'));
    expect([...RESERVED_TABLE_NAMES].sort()).toEqual(pythonSet('_RESERVED_TABLE_NAMES'));
    expect([...RESERVED_MODEL_SLUGS].sort()).toEqual(pythonSet('_RESERVED_MODEL_SLUGS'));
  });

  it('every plural the translator keeps is one the engine accepts', () => {
    const engineSlug = pythonRegex('_MODEL_SLUG_RE');
    const samples = ['a', 'posts', 'blog-posts', 'p2', 'a' + 'b'.repeat(59), 'a' + 'b'.repeat(60), '3d', 'blog_posts', '-x', 'x-', 'X', '', 'a.b', 'a/b'];
    for (const sample of samples) {
      if (MODEL_PLURAL_RE.test(sample)) expect(engineSlug.test(sample), sample).toBe(true);
    }
    // And the translator is not looser on shape, only shorter on length.
    for (const sample of samples.filter((x) => x.length <= 60)) {
      expect(MODEL_PLURAL_RE.test(sample), sample).toBe(engineSlug.test(sample));
    }
  });

  it('public_listing_path: same verdict as the engine on every sample', () => {
    const safePath = pythonRegex('_SAFE_PATH_RE');
    const reserved = new Set(pythonSet('_RESERVED_PUBLIC_PATHS'));
    // _public_listing_path_error, restated with the engine's own constants.
    const engineRefuses = (value: string): boolean => {
      const plain = value.length <= 120 && (value === '/' || safePath.test(value));
      if (!plain || value === '/') return true;
      const lowered = value.replace(/\/+$/, '').toLowerCase();
      return lowered === '/admin' || lowered.startsWith('/admin/') || reserved.has(lowered);
    };
    const samples = [
      '/', '//x', '/a//b', '/offerings//', 'offerings', '/../etc', '/offerings.html', '/offerings/:id', '/offerings/*',
      '/our offerings', "/o'", '/-o', '/a/b/c/d/e/f/g/h/i', '/' + 'a'.repeat(120), '/' + 'a'.repeat(119),
      '/admin', '/admin/', '/admin/x', '/Admin/x', '/ADMIN', '/administrators', '/login', '/Login', '/Contact/',
      '/uploads', '/uploads/gallery', '/health', '/offerings', '/our-offerings', '/Offerings', '/work/with_me',
      '/offerings/', '/a/b/c/d/e/f/g/h', '/js', '/brand', '/fonts', '/images', '/terms', '/privacy', '/magic',
      ...[...reserved],
      ...[...reserved].map((p) => p.toUpperCase() + '/'),
    ];
    for (const sample of samples) {
      expect(publicListingPathProblem(sample) !== null, sample).toBe(engineRefuses(sample));
    }
  });

  it('admin email and app name: the translator never emits what the engine refuses', () => {
    const engineEmail = pythonRegex('_ADMIN_EMAIL_RE');
    const engineAppName = pythonRegex('_APP_NAME_RE');
    for (const sample of ['maya@example.com', 'a.b+c@sub.example.org', 'maya@localhost', 'x@y', "m'@example.com", 'a b@example.com', '@example.com']) {
      expect(isEngineSafeAdminEmail(sample), sample).toBe(sample.length <= 254 && engineEmail.test(sample));
    }
    const reserved = source.match(/^_RESERVED_APP_NAME_PREFIX = "([a-z]+)"$/m);
    expect(reserved, '_RESERVED_APP_NAME_PREFIX in compose/build.py').not.toBeNull();
    for (const name of ['9 Sample Studio', 'Sample Studio', '--x--', '42', 'Zoe!', 'a'.repeat(90), '9' + 'a'.repeat(90),
                        'CraftAndShip', 'craftandship-web', ' Craftandship studio ', 'craftandship'.repeat(8)]) {
      const emitted = translateExtractedSpecToV1(baseInput({ app_name: name })).spec.app_name;
      expect(engineAppName.test(emitted), `${name} -> ${emitted}`).toBe(true);
      expect(emitted.startsWith(reserved![1]), `${name} -> ${emitted}`).toBe(false);
    }
  });

  it('custom-page rules: the reserved paths and the role-name limit are the engine\'s', () => {
    expect([...RESERVED_CUSTOM_PAGE_PATHS].sort()).toEqual(pythonSet('_RESERVED_CUSTOM_PAGE_PATHS'));
    expect(source).toMatch(new RegExp(`^_MAX_ROLE_NAME_LEN = ${MAX_ROLE_NAME_LENGTH}$`, 'm'));
  });

  describe('what the archetypes own (compose/data)', () => {
    interface ArchetypeFile {
      id: string;
      default_pages: string[];
      page_layouts: Record<string, string>;
      default_data_models: Array<{ name: string; plural?: string }>;
    }
    interface LayoutFile {
      variant_group?: string;
      widgets?: Array<{ id?: string; source?: string }>;
    }
    const dataDir = path.join(path.dirname(ENGINE_SOURCE_PATH), 'data');
    const readData = (name: string) =>
      engineAvailable ? JSON.parse(fs.readFileSync(path.join(dataDir, name), 'utf-8')) : {};
    const archetypes: ArchetypeFile[] = readData('archetypes.json').archetypes ?? [];
    const layouts: Record<string, LayoutFile> = readData('page-layouts.json').layouts ?? {};
    /** The table of a widget whose source is `db:<table>`, as `_db_source_table` reads it. */
    const dbTable = (widget: { source?: string }): string =>
      typeof widget.source === 'string' && widget.source.startsWith('db:') ? widget.source.slice(3).trim() : '';

    it('reads both decision files', () => {
      expect(archetypes.length).toBeGreaterThan(5);
      expect(Object.keys(layouts).length).toBeGreaterThan(10);
    });

    it('ARCHETYPE_MODEL_SLUGS is every default model of every archetype, by route slug', () => {
      const fromFiles: Record<string, string> = {};
      for (const archetype of archetypes) {
        for (const model of archetype.default_data_models) {
          const slug = model.plural ?? engineSlugForModelName(model.name);
          // One owner per slug over all archetypes, or the union would not be a rule.
          if (fromFiles[slug] !== undefined) expect(fromFiles[slug], slug).toBe(model.name);
          fromFiles[slug] = model.name;
        }
      }
      expect(ARCHETYPE_MODEL_SLUGS).toEqual(fromFiles);
    });

    it('ARCHETYPE_PAGE_SLUGS is every default page of every archetype', () => {
      const pages = new Set(archetypes.flatMap((archetype) => archetype.default_pages));
      expect([...ARCHETYPE_PAGE_SLUGS].sort()).toEqual([...pages].sort());
    });

    it('ARCHETYPE_SHOWN_TABLES is every table a page layout reads', () => {
      // Every layout in the file, not only the ones an archetype names: a
      // design hint can swap a page's layout for a sibling of its variant group.
      const tables = new Set<string>();
      for (const layout of Object.values(layouts)) {
        for (const widget of layout.widgets ?? []) {
          if (dbTable(widget)) tables.add(dbTable(widget));
        }
      }
      expect([...ARCHETYPE_SHOWN_TABLES].sort()).toEqual([...tables].sort());
    });

    it('ARCHETYPE_DETAIL_ROUTE_PAGES is every archetype page whose layout, or a sibling of it, links to a page per row', () => {
      const detailWidgets = source.match(/^_DETAIL_PAGE_WIDGETS = \(([^)]*)\)$/m);
      expect(detailWidgets).not.toBeNull();
      const detailIds = new Set([...detailWidgets![1].matchAll(/"([^"]+)"/g)].map((m) => m[1]));
      expect(detailIds.size).toBeGreaterThan(0);
      const hasDetailWidget = (layout: LayoutFile | undefined): boolean =>
        (layout?.widgets ?? []).some((widget) => detailIds.has(widget.id ?? '') && dbTable(widget) !== '');
      const pages = new Set<string>();
      for (const archetype of archetypes) {
        for (const [pageId, layoutId] of Object.entries(archetype.page_layouts)) {
          if (pageId === 'home') continue; // the home page has no /<page>/<row> route
          const group = layouts[layoutId]?.variant_group;
          const candidates = Object.values(layouts).filter(
            (layout) => layout === layouts[layoutId] || (group !== undefined && layout.variant_group === group)
          );
          if (candidates.some(hasDetailWidget)) pages.add(pageId);
        }
      }
      expect([...ARCHETYPE_DETAIL_ROUTE_PAGES].sort()).toEqual([...pages].sort());
    });
  });

  it('copiedPageRoute and the boolean rule restate compose/intake.py', () => {
    const intake = fs.readFileSync(path.join(path.dirname(ENGINE_SOURCE_PATH), 'intake.py'), 'utf-8');
    const copyId = intake.match(/def _copy_path_to_page_id\(path: str\) -> str:[\s\S]*?\n\n\n/)?.[0] ?? '';
    expect(copyId).toContain('p = path.strip().lower()');
    expect(copyId).toContain('if not p or p == "/":\n        return "home"');
    expect(copyId).toContain('slug = re.sub(r"[^a-z0-9_]+", "-", p).strip("-_")[:64].strip("-_")');
    expect(copyId).toContain('return slug or "page"');
    // The v1 field types that become a BOOLEAN column: exactly the two isBooleanField accepts.
    const typeMap = intake.match(/^_V1_FIELD_TYPE_TO_SQL = \{([\s\S]*?)^\}/m)?.[1] ?? '';
    const booleanTypes = [...typeMap.matchAll(/"([a-z]+)":\s*"BOOLEAN"/g)].map((m) => m[1]).sort();
    expect(booleanTypes).toEqual(['bool', 'boolean']);
    // And the engine's flag is a BOOLEAN column named "published", nothing else.
    const flag = source.match(/def _publication_column\(model: dict\) -> Optional\[str\]:[\s\S]*?\n\n\n/)?.[0] ?? '';
    expect(flag).toContain('_field_type_kind(field) == "boolean"');
    // ... where a column is boolean when its type is one of the two names, as isBooleanField decides.
    const kinds = source.match(/^_SQL_TYPE_KINDS = \{([\s\S]*?)^\}/m)?.[1] ?? '';
    expect([...kinds.matchAll(/"([a-z ]+)":\s*"boolean"/g)].map((m) => m[1]).sort()).toEqual(['bool', 'boolean']);
  });

  it('engineSlugForModelName restates _plural', () => {
    // def _plural(name): kebab = re.sub(r"([a-z])([A-Z])", r"\1-\2", name).lower(); y -> ies; s -> es; else + s
    const pluralSource = source.match(/def _plural\(name: str\) -> str:[\s\S]*?\n\n\n/)?.[0] ?? '';
    expect(pluralSource).toContain('re.sub(r"([a-z])([A-Z])", r"\\1-\\2", name).lower()');
    expect(pluralSource).toContain('if s.endswith("y") and not s[-2:-1] in "aeiou":');
    expect(pluralSource).toContain('return s[:-1] + "ies"');
    expect(pluralSource).toContain('if s.endswith("s"):');
    expect(pluralSource).toContain('return s + "es"');
    expect(pluralSource).toContain('return s + "s"');
  });
});


// ============================================================
// Columns the engine owns or reads with a fixed meaning, links between
// models, who may fill in a form, who may open a page, stock photos
// ============================================================

describe('harbor_to_compose: what the engine would refuse about columns, links, forms and pages', () => {
  const title = { name: 'title', type: 'string', required: true };

  function modelFields(input: ExtractedSpecInput, model: string) {
    const { spec } = translateExtractedSpecToV1(input);
    return spec.data_models?.find((m) => m.name === model)?.fields ?? [];
  }

  it('leaves id and a created_at that is not a date or timestamp to the engine, with a warning', () => {
    const input = baseInput({
      data_models: [{
        name: 'Note',
        fields: [
          { name: 'id', type: 'string' }, title,
          { name: 'created_at', type: 'string' }, { name: 'Updated At', type: 'string' },
        ],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    expect(spec.data_models?.[0].fields?.map((f) => f.name)).toEqual(['title', 'updated_at']);
    expect(warnings).toContain('Note.id: field dropped (the engine adds the primary key itself)');
    expect(warnings.some((w) => w.startsWith('Note.created_at: field dropped (the engine adds created_at as a timestamp'))).toBe(true);
  });

  it('keeps a created_at that is a date or a timestamp, and a published_at that is not json', () => {
    for (const field of [
      { name: 'created_at', type: 'timestamp' }, { name: 'created_at', type: 'date' },
      { name: 'created_at', sql_type: 'TIMESTAMPTZ' }, { name: 'published_at', type: 'string' },
      { name: 'published_at', sql_type: 'text' },
    ]) {
      const kept = modelFields(baseInput({ data_models: [{ name: 'Note', fields: [title, field] }] }), 'Note');
      expect(kept.map((f) => f.name), JSON.stringify(field)).toEqual(['title', field.name]);
    }
    const dropped = modelFields(
      baseInput({ data_models: [{ name: 'Note', fields: [title, { name: 'published_at', sql_type: 'JSONB' }] }] }),
      'Note'
    );
    expect(dropped.map((f) => f.name)).toEqual(['title']);
  });

  it('makes an email column a text column', () => {
    const fields = modelFields(
      baseInput({ data_models: [{ name: 'Lead', fields: [title, { name: 'email', type: 'integer' }] }] }),
      'Lead'
    );
    expect(fields[1]).toEqual({ name: 'email', type: 'email' });
  });

  it('drops a field named like the column that links a model to its parent, whatever its type', () => {
    const input = baseInput({
      data_models: [
        { name: 'Shelf', fields: [title] },
        { name: 'BlogComment', belongs_to: 'BlogPost', fields: [title, { name: 'blog_post_id', type: 'integer' }] },
        { name: 'Volume', belongs_to: 'shelves', plural: 'volumes', fields: [title, { name: 'shelf_id', type: 'string' }] },
      ],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    // BlogPost is an archetype model: the engine resolves the name there.
    expect(spec.data_models?.find((m) => m.name === 'BlogComment')?.fields?.map((f) => f.name)).toEqual(['title']);
    // Volume's parent is named by the plural of a spec model that has none declared: unresolved, so left alone.
    expect(spec.data_models?.find((m) => m.name === 'Volume')?.fields?.map((f) => f.name)).toEqual(['title', 'shelf_id']);
    expect(warnings.filter((w) => w.includes('the engine adds the column that links it to'))).toEqual([
      'data_model "BlogComment": field dropped (the engine adds the column that links it to "BlogPost" itself)',
    ]);
  });

  it('breaks a ring of models that belong to each other, once, and keeps every model', () => {
    const ring = baseInput({
      data_models: [
        { name: 'Alpha', belongs_to: 'Gamma', fields: [title] },
        { name: 'Beta', belongs_to: 'Alpha', fields: [title] },
        { name: 'Gamma', belongs_to: 'Beta', fields: [title] },
        { name: 'Category', belongs_to: 'Category', fields: [title] },
      ],
    });
    const { spec, warnings } = translateExtractedSpecToV1(ring);
    expect(spec.data_models?.map((m) => m.name)).toEqual(['Alpha', 'Beta', 'Gamma', 'Category']);
    // Followed from the first model the ring closes at Beta's link, so that is the one dropped.
    expect(spec.data_models?.filter((m) => m.belongs_to).map((m) => m.name)).toEqual(['Alpha', 'Gamma', 'Category']);
    expect(warnings).toContain(
      'data_model "Beta": belongs_to "Alpha" dropped (the models belong to each other in a ring, and a table cannot point to one that is created after it)'
    );
    expect(warnings.filter((w) => w.includes('belong to each other in a ring'))).toHaveLength(1);
    // A model that belongs to itself is a tree, not a ring.
    expect(spec.data_models?.find((m) => m.name === 'Category')?.belongs_to).toBe('Category');
  });

  it('names the columns of the form page of a public model, and never the engine\'s own', () => {
    const input = baseInput({
      pages: [{ slug: 'tell', form_target: 'Story' }],
      data_models: [{
        name: 'Story',
        fields: [
          title, { name: 'body', type: 'long_text' }, { name: 'featured_on_home', type: 'boolean' },
          { name: 'secret', type: 'password' }, { name: 'photo', type: 'image' }, { name: 'status', type: 'string' },
        ],
      }],
    });
    const { spec } = translateExtractedSpecToV1(input);
    const page = spec.pages?.find((p) => p.path === '/tell');
    // Shown to nobody (no listing, not an archetype table): no published field is needed.
    expect(page?.allowed_fields).toEqual(['title', 'body', 'featured_on_home']);
  });

  it('turns a form page whose model has no field a visitor may fill in into an ordinary page', () => {
    for (const model of [{ name: 'Rsvp' }, { name: 'Rsvp', fields: [{ name: 'status', type: 'string' }] }]) {
      const { spec, warnings } = translateExtractedSpecToV1(
        baseInput({ pages: [{ slug: 'rsvp', form_target: 'Rsvp', purpose: 'x'.repeat(60) }], data_models: [model] })
      );
      expect(spec.pages?.[0].form_target).toBeUndefined();
      expect(warnings.some((w) => w.includes('has no field of its own that a visitor may fill in'))).toBe(true);
    }
  });

  it('makes a page that lists roles a page that asks for a login', () => {
    const { spec, warnings } = translateExtractedSpecToV1(
      baseInput({ pages: [{ slug: 'staff-notes', allowed_roles: ['admin'] }, { slug: 'about-us', allowed_roles: [] }] })
    );
    expect(spec.pages?.find((p) => p.path === '/staff-notes')).toMatchObject({ auth_required: true, allowed_roles: ['admin'] });
    expect(spec.pages?.find((p) => p.path === '/about-us')?.auth_required).toBe(false);
    expect(warnings.filter((w) => w.includes('lists allowed_roles without auth_required'))).toHaveLength(1);
  });

  it('takes a path to a stock photo of a person out of the whole spec, and any other path stays', () => {
    const input = baseInput({
      copy_pages: [{
        path: '/about',
        markdown_blocks: [
          'Me: /images/stock/practitioner-portrait-01.jpg and /images/stock/aromatherapy-01.jpg and '
            + 'https://cdn.example.com/images/stock/event-gathering-01.jpg',
        ],
      }],
    });
    const { spec, warnings } = translateExtractedSpecToV1(input);
    const block = spec._harbor_meta?.copy_pages?.[0].markdown_blocks[0];
    expect(block).toBe(
      'Me:  and /images/stock/aromatherapy-01.jpg and https://cdn.example.com/images/stock/event-gathering-01.jpg'
    );
    expect(warnings.filter((w) => w.startsWith('a path to the stock photo "practitioner-portrait-01.jpg" was removed'))).toHaveLength(1);
  });
});

describe.skipIf(!engineAvailable)('translator rules match what the engine does with columns', () => {
  const engineDir = path.dirname(ENGINE_SOURCE_PATH);
  const source = engineAvailable ? fs.readFileSync(ENGINE_SOURCE_PATH, 'utf-8') : '';

  it('sqlTypeKind agrees with _SQL_TYPE_KINDS', () => {
    const table = source.match(/^_SQL_TYPE_KINDS = \{([\s\S]*?)^\}/m)?.[1] ?? '';
    const entries = [...table.matchAll(/"([a-z ]+)":\s*"([a-z]+)"/g)].map((m) => [m[1], m[2]] as const);
    expect(entries.length).toBeGreaterThan(20);
    for (const [name, kind] of entries) expect(sqlTypeKind(name), name).toBe(kind);
    expect(sqlTypeKind('varchar(40)')).toBe('text');
    expect(sqlTypeKind('NUMERIC(10, 2)')).toBe('numeric');
    expect(sqlTypeKind('text[]')).toBe('array');
    expect(sqlTypeKind('hstore')).toBe('other');
  });

  it('fieldKind agrees with the v1 type map of compose/intake.py', () => {
    const intake = fs.readFileSync(path.join(engineDir, 'intake.py'), 'utf-8');
    const typeMap = intake.match(/^_V1_FIELD_TYPE_TO_SQL = \{([\s\S]*?)^\}/m)?.[1] ?? '';
    const entries = [...typeMap.matchAll(/"([a-z]+)":\s*"([A-Z]+)(?:\(\d+\))?"/g)].map((m) => [m[1], m[2]] as const);
    expect(entries.length).toBeGreaterThan(10);
    const kinds = new Map<string, string>();
    for (const [name, kind] of sqlTypeKindsOf(source)) kinds.set(name, kind);
    for (const [v1Type, sql] of entries) expect(fieldKind({ name: 'x', type: v1Type }), v1Type).toBe(kinds.get(sql.toLowerCase()));
    // A word the map does not know is TEXT there.
    expect(fieldKind({ name: 'x', type: 'long_text' })).toBe('text');
    expect(fieldKind({ name: 'x' })).toBe('text');
  });

  function sqlTypeKindsOf(text: string): Array<readonly [string, string]> {
    const table = text.match(/^_SQL_TYPE_KINDS = \{([\s\S]*?)^\}/m)?.[1] ?? '';
    return [...table.matchAll(/"([a-z ]+)":\s*"([a-z]+)"/g)].map((m) => [m[1], m[2]] as const);
  }

  it('foreignKeyColumn restates the column of _belongs_to_info', () => {
    const info = source.match(/def _belongs_to_info\(rs: ResolvedSpec, model: dict\):[\s\S]*?\n\n\n/)?.[0] ?? '';
    expect(info).toContain('fk_col = _singular_slug(parent_model["name"]).replace("-", "_") + "_id"');
    const singular = source.match(/def _singular_slug\(name: str\) -> str:[\s\S]*?\n\n\n/)?.[0] ?? '';
    expect(singular).toContain('re.sub(r"([a-z])([A-Z])", r"\\1-\\2", name).lower()');
    expect(foreignKeyColumn('BlogPost')).toBe('blog_post_id');
    expect(foreignKeyColumn('FAQ')).toBe('faq_id');
    expect(foreignKeyColumn('StaffMember')).toBe('staff_member_id');
  });

  it('the columns a public form never offers are the engine\'s', () => {
    const block = source.match(/^_PUBLIC_FORM_SYSTEM_FIELDS = frozenset\(\{([\s\S]*?)\}\)/m)?.[1] ?? '';
    const engine = [...block.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]).sort();
    // The translator names a form's fields from the model's own: none of these may be among them.
    const input = baseInput({
      pages: [{ slug: 'tell', form_target: 'Story' }],
      data_models: [{ name: 'Story', fields: [{ name: 'title', type: 'string' }, ...engine.map((name) => ({ name, type: 'string' }))] }],
    });
    const page = translateExtractedSpecToV1(input).spec.pages?.find((p) => p.path === '/tell');
    expect(page?.allowed_fields).toEqual(['title']);
  });

  it('the stock photos of people are recognized as the engine recognizes them', () => {
    const stock = fs.readFileSync(path.join(engineDir, 'stock_photos.py'), 'utf-8');
    const pattern = stock.match(/_PERSONAL_PHOTO_PATTERN = re\.compile\(\s*((?:r"[^"]*"\s*)+),/)?.[1] ?? '';
    const joined = [...pattern.matchAll(/r"([^"]*)"/g)].map((m) => m[1]).join('');
    expect(joined).toBe('practitioner[\\s_-]?portrait|facilitator|gathering|dome[\\s_-]?venue|event[\\s_-]?venue');
    const manifest = JSON.parse(
      fs.readFileSync(path.join(engineDir, '..', 'cli', 'stock_photos', 'manifest.json'), 'utf-8')
    ) as Record<string, Array<{ filename: string }>>;
    const flagged = Object.values(manifest).flat().map((p) => p.filename).filter(isPersonalStockPhotoName).sort();
    expect(flagged).toEqual([
      'dome-venue-01.jpg', 'dome-venue-02.jpg', 'event-gathering-01.jpg', 'event-venue-01.jpg',
      'event-venue-02.jpg', 'facilitator-portrait-01.jpg', 'practitioner-portrait-01.jpg',
    ]);
  });
});
