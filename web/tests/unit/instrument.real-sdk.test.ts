// The real Sentry SDK, with the options src/instrument.ts gives it, in front of
// an Express app: what leaves the process when a request that carries
// credentials in its headers is traced, and when it ends in a 5xx.
//
// instrument.test.ts checks the options and the scrub function on hand-built
// events, with the SDK replaced. This file replaces nothing but the transport:
// Sentry.init receives sentryInitOptions() as production does, except that
// every request is traced (tracesSampleRate 1, production samples one in five)
// and envelopes are kept in memory instead of being sent. The DSN names a
// reserved host that resolves nowhere, and the transport never uses it.
//
// The headers are the shapes the app receives: the ops API key and the worker
// status hook's secret as `Authorization: Bearer`, Stripe's and Svix's
// signatures, the CSRF token, the session cookie, and a token in the query.
//
// The SDK prints "express is not instrumented" here: its Express patch needs
// Sentry to start before Node loads express, which a test file cannot arrange.
// Nothing below depends on it; the transactions come from the HTTP server
// instrumentation and the error event from setupExpressErrorHandler.

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import * as Sentry from '@sentry/node';
import express from 'express';
import request from 'supertest';

const run = String(Date.now());
const DSN = ['https://', 'public', '@', 'sentry.invalid', '/', '1'].join('');
const SECRETS = {
  bearer: ['ops', 'key', 'value', run].join('-'),
  cookie: ['sess', 'cookie', 'value', run].join('-'),
  stripeSignature: ['v1', ['5f', 'a1', 'b2', run].join('')].join('='),
  svixSignature: ['v1', ['c3', 'd4', 'e5', run].join('')].join(','),
  csrf: ['csrf', 'value', run].join('.'),
  queryToken: ['query', 'token', run].join('-'),
};

const envelopes: string[] = [];

/** The item types in one envelope: a header line, then an item header line and a payload line per item. */
function itemTypes(envelope: string): string[] {
  const lines = envelope.split('\n');
  const types: string[] = [];
  for (let index = 1; index < lines.length; index += 2) {
    if (!lines[index]) continue;
    const header = JSON.parse(lines[index]) as { type?: unknown };
    if (typeof header.type === 'string') types.push(header.type);
  }
  return types;
}

function sendWithCredentials(app: express.Express, path: string) {
  return request(app)
    .get(`${path}?token=${SECRETS.queryToken}`)
    .set('Authorization', `Bearer ${SECRETS.bearer}`)
    .set('Cookie', `cas.sid=${SECRETS.cookie}`)
    .set('stripe-signature', SECRETS.stripeSignature)
    .set('svix-signature', SECRETS.svixSignature)
    .set('x-csrf-token', SECRETS.csrf)
    .set('User-Agent', 'a-test-agent');
}

describe('instrument.ts with the real Sentry SDK and an in-memory transport', () => {
  beforeAll(async () => {
    // instrument.ts initializes Sentry on import when SENTRY_DSN is set; this
    // test initializes it itself, with a transport of its own.
    delete process.env.SENTRY_DSN;
    const { sentryInitOptions } = await import('../../src/instrument');
    const options = sentryInitOptions(DSN, 'production');
    expect(options.tracesSampleRate).toBe(0.2);
    Sentry.init({
      ...options,
      tracesSampleRate: 1,
      transport: (transportOptions) =>
        Sentry.createTransport(transportOptions, async (outgoing) => {
          envelopes.push(typeof outgoing.body === 'string' ? outgoing.body : Buffer.from(outgoing.body).toString('utf8'));
          return { statusCode: 200 };
        }),
    });

    const app = express();
    app.get('/ops/api/v1/ok', (_req, res) => {
      res.json({ ok: true });
    });
    app.get('/ops/api/v1/boom', () => {
      throw new Error('boom: a 5xx on the ops API');
    });
    Sentry.setupExpressErrorHandler(app);
    app.use((_err: Error, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
      res.status(500).json({ error: 'An unexpected error occurred.' });
    });

    expect((await sendWithCredentials(app, '/ops/api/v1/ok')).status).toBe(200);
    expect((await sendWithCredentials(app, '/ops/api/v1/boom')).status).toBe(500);
    expect(await Sentry.flush(5000)).toBe(true);
  });

  afterAll(async () => {
    await Sentry.close(2000);
  });

  it('the SDK did report both requests: a transaction for each and an error event for the 5xx', () => {
    const types = envelopes.flatMap(itemTypes);
    expect(types.filter((type) => type === 'transaction').length).toBeGreaterThanOrEqual(2);
    expect(types).toContain('event');
    const all = envelopes.join('\n');
    expect(all).toContain('/ops/api/v1/ok');
    expect(all).toContain('boom: a 5xx on the ops API');
    // A listed header does reach Sentry, as a span attribute: the scrub keeps
    // what identifies a fault and the SDK's header capture was exercised.
    expect(all).toContain('"http.request.header.user_agent":"a-test-agent"');
  });

  it('no envelope carries a credential the requests sent', () => {
    expect(envelopes.length).toBeGreaterThan(0);
    for (const envelope of envelopes) {
      for (const [name, value] of Object.entries(SECRETS)) {
        expect(envelope.includes(value), `${name} in a ${itemTypes(envelope).join('+')} envelope`).toBe(false);
      }
    }
  });
});
