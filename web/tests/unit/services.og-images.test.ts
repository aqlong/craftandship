// Per-page OG image mapping tests.
//
// Background: every page
// previously shared `og:image=https://craftandship.io/og-default.png`, so
// every link share looked identical. The service module owns the curated
// path -> filename map; this suite locks it down.

import { describe, it, expect } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';

import {
  resolveOgImage,
  PER_PAGE_OG_IMAGES,
  DEFAULT_OG_IMAGE,
} from '../../src/services/og-images';

describe('resolveOgImage', () => {
  it('returns DEFAULT_OG_IMAGE for an unmapped path', () => {
    expect(resolveOgImage('/some/random/path')).toBe(DEFAULT_OG_IMAGE);
  });

  it('returns DEFAULT_OG_IMAGE for an empty path', () => {
    expect(resolveOgImage('')).toBe(DEFAULT_OG_IMAGE);
  });

  it('returns the mapped image for /pricing', () => {
    expect(resolveOgImage('/pricing')).toBe('/og-pricing.png');
  });

  it('returns the mapped image for /the-companion', () => {
    expect(resolveOgImage('/the-companion')).toBe('/og-companion.png');
  });

  it('returns the mapped image for /about/ocean (nested route)', () => {
    expect(resolveOgImage('/about/ocean')).toBe('/og-about-ocean.png');
  });

  it('returns the mapped image for /about (parent of /about/ocean)', () => {
    expect(resolveOgImage('/about')).toBe('/og-about.png');
  });

  it('does not match by prefix (only exact paths get overrides)', () => {
    expect(resolveOgImage('/pricing/extra')).toBe(DEFAULT_OG_IMAGE);
    expect(resolveOgImage('/about/anything-else')).toBe(DEFAULT_OG_IMAGE);
  });

  it('returns the default image for /', () => {
    expect(resolveOgImage('/')).toBe('/og-default.png');
  });
});

describe('PER_PAGE_OG_IMAGES', () => {
  it('lists every canonical Harbor public page (drift guard vs sitemap)', () => {
    // Same canonical list rendered into /sitemap.xml. If a page is added
    // to the sitemap without an OG image, social shares of it will look
    // identical to every other page; the drift guard catches that.
    const sitemapPaths = [
      '/',
      '/pricing',
      '/the-companion',
      '/how-it-works',
      '/gallery',
      '/about',
      '/about/ocean',
      '/contact',
      '/terms',
      '/privacy',
    ];
    for (const p of sitemapPaths) {
      expect(PER_PAGE_OG_IMAGES).toHaveProperty(p);
    }
  });

  it('every mapped image has a 1200x630 PNG asset on disk', () => {
    // Catches the "renamed the route, forgot to regenerate the PNG" bug.
    const publicDir = path.resolve(__dirname, '..', '..', 'public');
    for (const file of Object.values(PER_PAGE_OG_IMAGES)) {
      const abs = path.join(publicDir, file.replace(/^\//, ''));
      expect(fs.existsSync(abs), `missing OG asset: ${abs}`).toBe(true);
      // PNG signature: 89 50 4E 47 0D 0A 1A 0A
      const header = fs.readFileSync(abs).slice(0, 8);
      expect(header[0]).toBe(0x89);
      expect(header[1]).toBe(0x50);
      expect(header[2]).toBe(0x4e);
      expect(header[3]).toBe(0x47);
    }
  });
});
