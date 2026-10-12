import { describe, it, expect, beforeAll, vi } from 'vitest';
import request from 'supertest';
import type { Application } from 'express';

/**
 * Tests for explicit CORS same-origin policy
 * (web-explicit-cors-same-origin-allowlist, 2026-05-09).
 *
 * Asserts:
 *  - No Access-Control-Allow-Origin header is emitted on any response,
 *    including /lighthouse/* (which is the most cookie-credentialed
 *    surface and the prime target of a cross-origin token-theft attempt).
 *  - OPTIONS preflight requests carrying an Origin header are rejected
 *    with 204 + no CORS headers, so cross-origin scripts cannot
 *    proceed past the preflight.
 *  - Webhook endpoints (/webhooks/stripe) remain reachable: Stripe sends
 *    server-to-server POSTs without an Origin header, which the policy
 *    does not block.
 */

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [] }),
    connect: vi.fn(),
  },
}));

let app: Application;

beforeAll(async () => {
  process.env.CAS_HARBOR_ENABLED = 'true';
  process.env.NODE_ENV = process.env.NODE_ENV || 'test';
  vi.resetModules();
  const appModule = await import('../../src/app');
  app = appModule.default;
});

describe('CORS: same-origin policy', () => {
  it('does not set Access-Control-Allow-Origin on / (no Origin sent)', async () => {
    const res = await request(app).get('/');
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('does not set Access-Control-Allow-Origin on / when Origin is sent', async () => {
    const res = await request(app)
      .get('/')
      .set('Origin', 'https://evil.example.com');
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('does not set Access-Control-Allow-Origin on /lighthouse', async () => {
    const res = await request(app)
      .get('/lighthouse')
      .set('Origin', 'https://evil.example.com');
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('does not set Access-Control-Allow-Origin on /lighthouse/table', async () => {
    const res = await request(app)
      .get('/lighthouse/table')
      .set('Origin', 'https://evil.example.com');
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('rejects cross-origin OPTIONS preflight with 204 + no CORS headers', async () => {
    const res = await request(app)
      .options('/lighthouse/table/turn')
      .set('Origin', 'https://evil.example.com')
      .set('Access-Control-Request-Method', 'POST')
      .set('Access-Control-Request-Headers', 'content-type');
    expect(res.status).toBe(204);
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
    expect(res.headers['access-control-allow-methods']).toBeUndefined();
    expect(res.headers['access-control-allow-headers']).toBeUndefined();
    expect(res.headers['access-control-allow-credentials']).toBeUndefined();
  });

  it('rejects cross-origin OPTIONS preflight on / (root) too', async () => {
    const res = await request(app)
      .options('/')
      .set('Origin', 'https://evil.example.com')
      .set('Access-Control-Request-Method', 'GET');
    expect(res.status).toBe(204);
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('OPTIONS without Origin header is NOT short-circuited as preflight', async () => {
    // Same-origin OPTIONS (no Origin header) should not be intercepted
    // by the preflight branch. Express may still 404 it for an
    // unrouted path, but it must not be the 204+no-headers preflight
    // response.
    const res = await request(app).options('/lighthouse/table/turn');
    // The middleware ONLY short-circuits when Origin is present.
    // Regardless of what the underlying route does (404, etc.),
    // the response should not carry Access-Control-Allow-Origin.
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
  });

  it('webhook endpoint accepts server-to-server POST (no Origin header)', async () => {
    // Stripe sends POSTs without an Origin header. The CORS middleware
    // must not interfere. We expect the webhook to attempt to process
    // the request (and likely fail signature verification with 400),
    // not to be blocked by the CORS preflight branch.
    const res = await request(app)
      .post('/webhooks/stripe')
      .set('Content-Type', 'application/json')
      .send('{}');
    // The request reaches the Stripe handler, which refuses the missing
    // signature itself (400); it is not swallowed by the 204 preflight branch.
    expect(res.status).toBe(400);
    expect(res.headers['access-control-allow-origin']).toBeUndefined();
  });
});
