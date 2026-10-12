// A script is answered in JSON, a page in HTML, also when the answer does not
// come from a route: a body the parser refuses, a missing route, a form token
// that does not verify, a limiter. The rule is isJsonRequest
// (utils/json-request.ts); the second half of this file sends real requests
// through the real app (the database is the only double).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import path from 'path';
import express from 'express';
import request from 'supertest';
import type { Request } from 'express';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return { default: { query: mockQuery, on: vi.fn(), connect: vi.fn() } };
});
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn((email: string) => `token-for-${email}`),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));

import app from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import { makeCustomerLlmDailyLimit } from '../../src/middleware/llm-rate-limit';
import { isJsonRequest, sendJsonError } from '../../src/utils/json-request';
import { clientErrorSentence } from '../../src/utils/error-page';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const webRoot = path.join(__dirname, '../..');

type Shape = { method?: string; url: string; headers?: Record<string, string> };
const asked = (shape: Shape): boolean =>
  isJsonRequest({ method: shape.method ?? 'GET', originalUrl: shape.url, headers: shape.headers ?? {} } as never);

describe('isJsonRequest: the one rule', () => {
  it('a request that says so: a JSON content type, or the token header our own fetch calls send', () => {
    for (const type of ['application/json', 'application/json; charset=utf-8', 'APPLICATION/JSON', 'application/vnd.api+json', ' application/json ']) {
      expect(asked({ method: 'POST', url: '/login', headers: { 'content-type': type } }), type).toBe(true);
    }
    expect(asked({ method: 'POST', url: '/guide', headers: { 'x-csrf-token': 'abc.def' } })).toBe(true);
  });

  it('a form, a page, an upload and a look-alike content type are not', () => {
    for (const type of ['application/x-www-form-urlencoded', 'multipart/form-data; boundary=x', 'text/plain', 'application/jsonp', 'text/json-ish', 'application/json5']) {
      expect(asked({ method: 'POST', url: '/guide', headers: { 'content-type': type } }), type).toBe(false);
    }
    expect(asked({ url: '/pricing' })).toBe(false);
    expect(asked({ url: '/' })).toBe(false);
  });

  it('a path under a prefix where every route answers JSON, whatever the method or body', () => {
    for (const url of ['/api/builds/abc', '/api/support/chat', '/ops/api/v1/status', '/webhooks/stripe', '/webhooks/resend', '/admin/api/costs?range=7d']) {
      expect(asked({ url }), url).toBe(true);
      expect(asked({ method: 'POST', url }), url).toBe(true);
    }
    // The prefix is a path segment, not a string that merely starts the same.
    for (const url of ['/apiary', '/api', '/webhooksy/x', '/ops/apix', '/admin/builds']) {
      expect(asked({ url }), url).toBe(false);
    }
  });

  it('the fetch endpoints of the conversation, intake and chat pages, as POSTs only', () => {
    const endpoints = [
      '/lighthouse/table/turn',
      '/lighthouse/build',
      '/lighthouse/bypass',
      '/lighthouse/preview/choose',
      '/intake/start',
      '/intake/message',
      '/intake/0b0e7a56-2a51-4a51-8a51-000000000001/logo',
      '/try/start',
      '/try/0b0e7a56-2a51-4a51-8a51-000000000001/message',
      '/builds/0b0e7a56-2a51-4a51-8a51-000000000001/revision/message',
      '/features/0b0e7a56-2a51-4a51-8a51-000000000001/message',
      '/feedback',
    ];
    for (const url of endpoints) {
      expect(asked({ method: 'POST', url }), url).toBe(true);
      // The same address read as a page is a page.
      expect(asked({ method: 'GET', url }), `GET ${url}`).toBe(false);
    }
    // Their neighbours are pages and forms.
    for (const url of ['/lighthouse/table', '/lighthouse/lamp/abc/make-public', '/intake/abc', '/try/abc', '/builds/abc/revision', '/builds/abc/revision/confirm', '/builds/abc/approve', '/features/abc/approve']) {
      expect(asked({ method: 'POST', url }), url).toBe(false);
    }
  });

  it('?format=json, exactly, in the query', () => {
    expect(asked({ url: '/lighthouse/table/recall?format=json' })).toBe(true);
    expect(asked({ url: '/lighthouse/table/recall?dev_trial=x&format=json' })).toBe(true);
    expect(asked({ url: '/lighthouse/table/recall' })).toBe(false);
    expect(asked({ url: '/lighthouse/table/recall?format=jsonp' })).toBe(false);
    expect(asked({ url: '/lighthouse/table/recall?informat=json' })).toBe(false);
  });

  it('reads the path the client sent, so it gives one answer inside a mounted router and outside it', () => {
    // Inside the router mounted at /intake, req.url is "/message"; originalUrl is the whole path.
    expect(isJsonRequest({ method: 'POST', url: '/message', originalUrl: '/intake/message', headers: {} } as never)).toBe(true);
    expect(isJsonRequest({ method: 'POST', url: '/message', headers: {} } as never)).toBe(false);
  });

  it('takes a header of any length in linear time', () => {
    const long = `application/${'a+'.repeat(40_000)}json`;
    const started = Date.now();
    asked({ method: 'POST', url: '/guide', headers: { 'content-type': long } });
    asked({ method: 'POST', url: `/${'a/'.repeat(40_000)}` });
    expect(Date.now() - started).toBeLessThan(200);
  });
});

describe('sendJsonError', () => {
  function sent(status: number, sentence?: string): { status: number; body: Record<string, string> } {
    const out = { status: 0, body: {} as Record<string, string> };
    sendJsonError(
      {
        status(code: number) {
          out.status = code;
          return this;
        },
        json(body: Record<string, string>) {
          out.body = body;
          return this;
        },
      } as never,
      status,
      sentence
    );
    return out;
  }

  it('a 4xx says what the error page says for that status, under error and message, with a code', () => {
    for (const [status, code] of [[400, 'bad_request'], [404, 'not_found'], [413, 'payload_too_large'], [429, 'rate_limited']] as const) {
      const out = sent(status);
      expect(out.status).toBe(status);
      expect(out.body).toEqual({ error: clientErrorSentence(status), message: clientErrorSentence(status), code });
    }
    expect(sent(418).body.code).toBe('request_refused');
  });

  it('a 5xx is a server error, with the sentence it is given or a plain default', () => {
    expect(sent(500).body).toEqual({ error: 'An unexpected error occurred.', message: 'An unexpected error occurred.', code: 'server_error' });
    expect(sent(500, 'the detail').body.message).toBe('the detail');
  });
});

describe('through the app: a body the parser refuses', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
  });

  const expectJson = (res: request.Response, status: number, code: string): void => {
    expect(res.status).toBe(status);
    expect(res.headers['content-type']).toMatch(/^application\/json/);
    expect(res.body.code).toBe(code);
    expect(res.body.error).toBe(res.body.message);
    expect(res.text).not.toMatch(/<!DOCTYPE|<html/i);
  };

  it.each([
    ['/api/support/chat'],
    ['/webhooks/resend'],
    ['/try/start'],
    ['/lighthouse/table/turn'],
    ['/intake/message'],
    // A form route, sent JSON: the content type decides.
    ['/login'],
    ['/guide'],
  ])('a malformed JSON body to %s is a 400 in JSON, and is not logged as a server fault', async (route) => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await request(app).post(route).set('Content-Type', 'application/json').send('{"message": ');
      expectJson(res, 400, 'bad_request');
      expect(res.body.message).toMatch(/could not be processed/);
      expect(errorSpy.mock.calls.some((call) => String(call[0]).includes('Unhandled error'))).toBe(false);
      expect(mockQuery).not.toHaveBeenCalled();
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('a JSON body over the general limit is a 413 in JSON', async () => {
    const res = await request(app)
      .post('/try/start')
      .set('Content-Type', 'application/json')
      .send(JSON.stringify({ message: 'x'.repeat(1.2 * 1024 * 1024) }));
    expectJson(res, 413, 'payload_too_large');
    expect(res.body.message).toMatch(/send less/);
  });

  it('a JSON body over the account forms\' own limit is a 413 in JSON; the same size as a form is the 413 page', async () => {
    const big = 'x'.repeat(12 * 1024);
    expectJson(await request(app).post('/login').set('Content-Type', 'application/json').send(JSON.stringify({ email: big })), 413, 'payload_too_large');
    const asForm = await request(app).post('/login').type('form').send(`email=${big}`);
    expect(asForm.status).toBe(413);
    expect(asForm.headers['content-type']).toMatch(/^text\/html/);
    expect(asForm.text).toContain('That was too large to accept.');
  });
});

describe('through the app: the 404 handler, the token check and the limiter', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
  });

  it('a missing route under /api/ is a 404 in JSON; a missing page is the 404 page', async () => {
    const api = await request(app).get('/api/no-such-thing');
    expect(api.status).toBe(404);
    expect(api.headers['content-type']).toMatch(/^application\/json/);
    expect(api.body.code).toBe('not_found');

    const page = await request(app).get('/no-such-page');
    expect(page.status).toBe(404);
    expect(page.headers['content-type']).toMatch(/^text\/html/);
    expect(page.text).toContain('That page is missing.');
  });

  it('a script\'s post without a valid token is a 403 in JSON, by its body as well as by the header; a form\'s is the 403 page', async () => {
    const byBody = await request(app).post('/guide').send({ email: 'person@example.com' });
    expect(byBody.status).toBe(403);
    expect(byBody.headers['content-type']).toMatch(/^application\/json/);
    expect(byBody.body.error).toMatch(/Invalid CSRF token/);

    const byHeader = await request(app).post('/guide').type('form').set('x-csrf-token', 'not-a-token').send({ email: 'person@example.com' });
    expect(byHeader.status).toBe(403);
    expect(byHeader.headers['content-type']).toMatch(/^application\/json/);

    const form = await request(app).post('/guide').type('form').send({ email: 'person@example.com' });
    expect(form.status).toBe(403);
    expect(form.headers['content-type']).toMatch(/^text\/html/);
    expect(form.text).toContain('could not be verified');

    // With a valid token the same JSON post is accepted.
    const ok = await request(app).post('/guide').send({ email: 'person@example.com', _csrf: generateCsrfToken({} as Request) });
    expect(ok.status).toBe(200);
  });

  it('the per-customer model limit answers a form with the 429 page and a script with JSON, by the same rule', async () => {
    const mini = express();
    mini.set('view engine', 'ejs');
    mini.set('views', path.join(webRoot, 'views'));
    mini.use(express.json(), express.urlencoded({ extended: true }));
    mini.use((req, _res, next) => {
      (req as unknown as { session: { customerId?: string } }).session = { customerId: 'customer-1' };
      next();
    });
    const limit = makeCustomerLlmDailyLimit({ max: 0 });
    // The form that starts a feature request, and the chat endpoint next to it.
    mini.post('/projects/:buildId/features/new', limit, (_req, res) => res.send('started'));
    mini.post('/features/:featureId/message', limit, (_req, res) => res.json({ ok: true }));

    const form = await request(mini).post('/projects/b-1/features/new').type('form').send({ title: 'A booking page' });
    expect(form.status).toBe(429);
    expect(form.headers['content-type']).toMatch(/^text\/html/);
    expect(form.text).toContain('daily limit for conversations');

    // No body at all: the route itself is what says this caller is a script.
    const script = await request(mini).post('/features/f-1/message');
    expect(script.status).toBe(429);
    expect(script.headers['content-type']).toMatch(/^application\/json/);
    expect(script.body.error).toMatch(/daily limit/);
  });
});
