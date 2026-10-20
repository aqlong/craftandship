import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import express from 'express';
import request from 'supertest';
import { publicBaseUrl } from '../../src/routes/auth';

// publicBaseUrl builds the origin of every emailed verification and reset
// link, and the Stripe return address. These tests drive it through a real
// Express request with 'trust proxy' set as app.ts sets it by default (one
// hop), so req.protocol is whatever X-Forwarded-Proto the client sent.

vi.mock('../../src/db/index', () => ({
  default: { query: vi.fn(), on: vi.fn() },
}));

function probeApp(hops: number): express.Express {
  const probe = express();
  probe.set('trust proxy', hops);
  probe.get('/origin', (req, res) => {
    res.type('text/plain').send(publicBaseUrl(req));
  });
  return probe;
}

describe('publicBaseUrl', () => {
  let savedBaseUrl: string | undefined;

  beforeEach(() => {
    savedBaseUrl = process.env.BASE_URL;
    delete process.env.BASE_URL;
  });

  afterEach(() => {
    if (savedBaseUrl === undefined) delete process.env.BASE_URL;
    else process.env.BASE_URL = savedBaseUrl;
  });

  it('prefers a configured BASE_URL over anything in the request', async () => {
    process.env.BASE_URL = 'https://app.example.test/';
    const res = await request(probeApp(1))
      .get('/origin')
      .set('Host', 'evil.example')
      .set('X-Forwarded-Proto', 'javascript');
    expect(res.text).toBe('https://app.example.test');
  });

  it('keeps https from a trusted proxy', async () => {
    const res = await request(probeApp(1))
      .get('/origin')
      .set('Host', 'app.example.test')
      .set('X-Forwarded-Proto', 'https');
    expect(res.text).toBe('https://app.example.test');
  });

  it('writes http for any other forwarded scheme, so a client cannot choose the link scheme', async () => {
    for (const proto of ['javascript', 'data', 'file', 'HTTPS ', 'ftp']) {
      const res = await request(probeApp(1))
        .get('/origin')
        .set('Host', 'app.example.test')
        .set('X-Forwarded-Proto', proto);
      expect(res.text, `X-Forwarded-Proto: ${proto}`).toBe('http://app.example.test');
    }
  });

  it('falls back to localhost for a Host that is not a plain host[:port]', async () => {
    const res = await request(probeApp(1))
      .get('/origin')
      .set('Host', 'evil.example/phish')
      .set('X-Forwarded-Proto', 'https');
    expect(res.text).toBe('https://localhost');
  });
});
