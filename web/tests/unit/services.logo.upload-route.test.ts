import { describe, it, expect, vi, beforeEach } from 'vitest';
import supertest from 'supertest';
import bcrypt from 'bcrypt';
import type { Request } from 'express';

// The database is the one thing replaced: the route, the multipart parser,
// the logo service and its cleaning thread are the real ones.
vi.mock('../../src/db/index', () => ({ default: { query: vi.fn(), on: vi.fn() } }));
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello.'),
}));

import app from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';

/**
 * `POST /intake/:buildId/logo` with an SVG, from the HTTP request to what is
 * written to the build row and sent back. The uploads here are the ones the
 * service-level tests use on their own: an SVG with active content, and one
 * that is ruinous to draw (250 chained masks: the version of logo.ts that
 * still drew the cleaned SVG died on it, taking the test process with it).
 */

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const NS = 'xmlns="http://www.w3.org/2000/svg"';

async function loggedInAgent(): Promise<supertest.Agent> {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('route-test-password', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'customer-logo', email: 'logo@example.com', name: 'Logo Tester', password: hashed, email_verified: true }],
  });
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: generateCsrfToken({} as Request), email: 'logo@example.com', password: 'route-test-password' });
  return agent;
}

async function upload(agent: supertest.Agent, svg: string): Promise<supertest.Response> {
  // The route reads the build, then writes the spec.
  mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-logo', spec: { design: { style: 'modern light' } }, status: 'intake' }] });
  mockQuery.mockResolvedValueOnce({ rows: [] });
  return agent
    .post('/intake/build-logo/logo')
    .set('x-csrf-token', generateCsrfToken({} as Request))
    .attach('logo', Buffer.from(svg, 'utf8'), { filename: 'logo.svg', contentType: 'image/svg+xml' });
}

function storedUpdate(): { spec: { design: Record<string, string> }; filename: string; mimetype: string } {
  const call = mockQuery.mock.calls.find((args: unknown[]) => String(args[0]).startsWith('UPDATE builds SET spec'));
  expect(call).toBeDefined();
  const params = call![1] as string[];
  return { spec: JSON.parse(params[0]), filename: params[1], mimetype: params[2] };
}

function decodeDataUri(uri: string): string {
  const prefix = 'data:image/svg+xml;base64,';
  expect(uri.startsWith(prefix)).toBe(true);
  return Buffer.from(uri.slice(prefix.length), 'base64').toString('utf8');
}

describe('POST /intake/:buildId/logo with an SVG', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('stores the cleaned SVG with a palette and size read from its text, and answers the same', async () => {
    const agent = await loggedInAgent();
    const res = await upload(
      agent,
      `<svg ${NS} viewBox="0 0 120 40" onload="alert(1)"><script>alert(2)</script>` +
        '<rect width="120" height="40" fill="#1d4ed8"/><circle cx="20" cy="20" r="12" fill="#f59e0b"/></svg>'
    );
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({
      success: true,
      width: 120,
      height: 40,
      colors: { primary_color: '#2040e0', secondary_color: '#ffa000' },
    });
    // The shape the intake page accepts before it assigns the preview.
    expect(res.body.logo_preview).toMatch(/^data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+$/);

    const stored = storedUpdate();
    expect(stored.mimetype).toBe('image/svg+xml');
    expect(stored.spec.design).toMatchObject({
      style: 'modern light',
      primary_color: '#2040e0',
      secondary_color: '#ffa000',
      logo_data: res.body.logo_preview,
    });
    const svg = decodeDataUri(stored.spec.design.logo_data);
    expect(svg).not.toMatch(/<script|onload|alert/);
    expect(svg).toContain('<circle cx="20" cy="20" r="12" fill="#f59e0b"/>');
  });

  it('an SVG that is ruinous to draw is stored like any other, and the server answers the next request', async () => {
    let masks = '';
    for (let level = 0; level < 250; level += 1) {
      const previous = level ? ` mask="url(#m${level - 1})"` : '';
      masks += `<mask id="m${level}"><rect width="40" height="40" fill="#fff"${previous}/></mask>`;
    }
    const agent = await loggedInAgent();
    const started = performance.now();
    const res = await upload(
      agent,
      `<svg ${NS} width="40" height="40">${masks}<rect width="40" height="40" fill="#b45309" mask="url(#m249)"/></svg>`
    );
    expect(res.status).toBe(200);
    expect(performance.now() - started).toBeLessThan(2000);
    expect(res.body).toMatchObject({ width: 40, height: 40, colors: { primary_color: '#c06000' } });
    expect(decodeDataUri(storedUpdate().spec.design.logo_data)).toContain('mask="url(#m249)"');

    const health = await supertest(app).get('/health');
    expect(health.status).toBeLessThan(500);
  });

  it('a file that is not an SVG document is a JSON 400 and nothing is written', async () => {
    const agent = await loggedInAgent();
    const res = await upload(agent, '<html><body><svg></svg></body></html>');
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/Only PNG, JPEG, SVG, and WebP/);
    expect(mockQuery.mock.calls.some((args: unknown[]) => String(args[0]).startsWith('UPDATE builds SET spec'))).toBe(false);
  });
});
