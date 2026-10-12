import { describe, it, expect, beforeAll } from 'vitest';
import request from 'supertest';
import type { Application } from 'express';
import { vi } from 'vitest';

/**
 * Tests for the security-header CSP nonce migration
 * (harden-csp-remove-unsafe-inline, 2026-05-09).
 *
 * `unsafe-inline` removed from script-src; replaced with a per-request
 * nonce-{N} source. Every inline `<script>` tag in views must carry
 * `nonce="<%= cspNonce %>"`. style-src retains `unsafe-inline` for
 * pervasive inline `style="..."` attributes (tracked separately).
 */

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [] }),
    connect: vi.fn(),
  },
}));

let app: Application;

beforeAll(async () => {
  process.env.NODE_ENV = process.env.NODE_ENV || 'test';
  vi.resetModules();
  const appModule = await import('../../src/app');
  app = appModule.default;
});

describe('CSP header: nonce migration', () => {
  it('script-src no longer carries unsafe-inline', async () => {
    const res = await request(app).get('/');
    const csp = res.headers['content-security-policy'];
    expect(csp).toBeTruthy();
    // Pull out script-src specifically.
    const scriptSrc = csp.match(/script-src ([^;]+);/)?.[1] || '';
    expect(scriptSrc).not.toContain("'unsafe-inline'");
    expect(scriptSrc).toContain("'self'");
    expect(scriptSrc).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
    expect(scriptSrc).toContain('https://plausible.io');
  });

  it('style-src intentionally retains unsafe-inline (Phase 1 carve-out)', async () => {
    const res = await request(app).get('/');
    const csp = res.headers['content-security-policy'];
    const styleSrc = csp.match(/style-src ([^;]+);/)?.[1] || '';
    expect(styleSrc).toContain("'unsafe-inline'");
    expect(styleSrc).toContain("'self'");
    expect(styleSrc).toContain('https://fonts.googleapis.com');
  });

  it('two requests get distinct nonces (not a static value)', async () => {
    const a = (await request(app).get('/')).headers['content-security-policy'];
    const b = (await request(app).get('/')).headers['content-security-policy'];
    const noncea = a.match(/'nonce-([A-Za-z0-9+/=]+)'/)?.[1];
    const nonceb = b.match(/'nonce-([A-Za-z0-9+/=]+)'/)?.[1];
    expect(noncea).toBeTruthy();
    expect(nonceb).toBeTruthy();
    expect(noncea).not.toEqual(nonceb);
  });

  it('nonce is high-entropy (>=24 base64 chars => >=18 random bytes)', async () => {
    const res = await request(app).get('/');
    const csp = res.headers['content-security-policy'];
    const nonce = csp.match(/'nonce-([A-Za-z0-9+/=]+)'/)?.[1] || '';
    expect(nonce.length).toBeGreaterThanOrEqual(24);
  });

  it('rendered HTML stamps the nonce on inline script tags', async () => {
    const res = await request(app).get('/');
    const csp = res.headers['content-security-policy'];
    const nonce = csp.match(/'nonce-([A-Za-z0-9+/=]+)'/)?.[1] || '';
    expect(nonce).toBeTruthy();
    // Every <script> tag (inline or with src) carries a nonce attribute
    // matching the response CSP nonce. The page must have some: otherwise
    // the loop below would pass without checking anything.
    const scriptOpenTags = res.text.match(/<script\b[^>]*>/g) || [];
    expect(scriptOpenTags.length).toBeGreaterThan(0);
    // The structured-data block is built by jsonLdScriptTag(..., cspNonce) in
    // app.ts; make sure it is among the tags being checked.
    expect(scriptOpenTags.some((tag) => tag.includes('application/ld+json'))).toBe(true);
    for (const tag of scriptOpenTags) {
      expect(tag).toContain(`nonce="${nonce}"`);
    }
  });

  it('other security headers remain in place', async () => {
    const res = await request(app).get('/');
    expect(res.headers['x-content-type-options']).toBe('nosniff');
    expect(res.headers['x-frame-options']).toBe('DENY');
    expect(res.headers['referrer-policy']).toBe('strict-origin-when-cross-origin');
    expect(res.headers['permissions-policy']).toBeTruthy();
  });
});
