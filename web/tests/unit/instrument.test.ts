// src/instrument.ts is the first import of server.ts, so it is the first code
// to read the environment. It has to load web/.env itself: otherwise a
// SENTRY_DSN that is set only in that file is read as unset and error
// reporting stays off without a word.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Built at run time; the host is a reserved name that resolves nowhere.
const DSN_FROM_FILE = ['https://', 'public', '@', 'sentry.invalid', '/', '1'].join('');

const order: string[] = [];
const init = vi.hoisted(() => vi.fn());

// httpIntegration returns what it was configured with, so a test can read it
// back out of the options given to init.
vi.mock('@sentry/node', () => ({
  init,
  httpIntegration: (options: Record<string, unknown>) => ({ name: 'Http', options }),
}));
// Stands in for web/.env: the variable exists only once dotenv has run.
vi.mock('dotenv', () => {
  const config = () => {
    order.push('dotenv.config');
    if (process.env.A3_DOTENV_PROVIDES_DSN === '1') process.env.SENTRY_DSN = DSN_FROM_FILE;
    return { parsed: {} };
  };
  return { default: { config }, config };
});

const saved = { dsn: process.env.SENTRY_DSN, nodeEnv: process.env.NODE_ENV };

beforeEach(() => {
  order.length = 0;
  init.mockReset().mockImplementation(() => {
    order.push('Sentry.init');
  });
  delete process.env.SENTRY_DSN;
  vi.resetModules();
});

afterEach(() => {
  delete process.env.A3_DOTENV_PROVIDES_DSN;
  if (saved.dsn === undefined) delete process.env.SENTRY_DSN;
  else process.env.SENTRY_DSN = saved.dsn;
  process.env.NODE_ENV = saved.nodeEnv;
});

describe('instrument.ts', () => {
  it('initializes Sentry from a DSN that only the .env file provides', async () => {
    process.env.A3_DOTENV_PROVIDES_DSN = '1';
    await import('../../src/instrument');
    expect(order).toEqual(['dotenv.config', 'Sentry.init']);
    expect(init.mock.calls[0][0].dsn).toBe(DSN_FROM_FILE);
  });

  it('stays off when no DSN is set anywhere', async () => {
    await import('../../src/instrument');
    expect(order).toEqual(['dotenv.config']);
    expect(init).not.toHaveBeenCalled();
  });

  it('sends no default PII and scrubs every event and transaction', async () => {
    process.env.A3_DOTENV_PROVIDES_DSN = '1';
    await import('../../src/instrument');
    const options = init.mock.calls[0][0];
    expect(options.sendDefaultPii).toBe(false);
    const secret = ['tok', 'en', 'value', '0123456789abcdef'].join('');
    const event = { request: { url: `https://example.com/reset-password?token=${secret}`, query_string: `token=${secret}` } };
    expect(JSON.stringify(options.beforeSend(structuredClone(event)))).not.toContain(secret);
    expect(JSON.stringify(options.beforeSendTransaction(structuredClone(event)))).not.toContain(secret);
  });

  it('what the visitor sent does not leave the process: no request body, no cookies, in events or in span data', async () => {
    process.env.A3_DOTENV_PROVIDES_DSN = '1';
    await import('../../src/instrument');
    const options = init.mock.calls[0][0];
    // The integration that would record the body of an incoming request is told not to.
    expect(options.integrations).toEqual([{ name: 'Http', options: { maxIncomingRequestBodySize: 'none' } }]);

    const said = `I have been thinking about ending it ${Date.now()}`;
    const typedPassword = ['pw', 'typed', 'by', 'a', 'visitor'].join('-');
    const cookieValue = ['c0ffee', 'c0ffee', 'c0ffee'].join('');
    const event = {
      request: {
        url: 'https://example.com/lighthouse/table/turn',
        data: JSON.stringify({ keeperText: said, password: typedPassword }),
        cookies: { cas_harbor_trial: cookieValue },
        headers: { cookie: `cas_harbor_trial=${cookieValue}`, 'user-agent': 'a browser' },
      },
      contexts: { trace: { data: { 'http.request.body.data': JSON.stringify({ keeperText: said }), 'http.request.header.cookie': cookieValue } } },
      spans: [{ data: { 'http.request.body.data': said, 'http.method': 'POST' } }],
    };
    for (const hook of [options.beforeSend, options.beforeSendTransaction]) {
      const out = hook(structuredClone(event));
      const sent = JSON.stringify(out);
      expect(sent).not.toContain('ending it');
      expect(sent).not.toContain(typedPassword);
      expect(sent).not.toContain(cookieValue);
      // What identifies the fault is still there.
      expect(out.request.url).toBe('https://example.com/lighthouse/table/turn');
      expect(out.request.headers['user-agent']).toBe('a browser');
      expect(out.spans[0].data['http.method']).toBe('POST');
    }
  });

  it('labels an unset NODE_ENV as production, like the rest of the app', async () => {
    process.env.A3_DOTENV_PROVIDES_DSN = '1';
    delete process.env.NODE_ENV;
    await import('../../src/instrument');
    expect(init.mock.calls[0][0].environment).toBe('production');
    expect(init.mock.calls[0][0].tracesSampleRate).toBe(0.2);

    vi.resetModules();
    init.mockClear();
    delete process.env.SENTRY_DSN;
    process.env.NODE_ENV = 'development';
    await import('../../src/instrument');
    expect(init.mock.calls[0][0].environment).toBe('development');
    expect(init.mock.calls[0][0].tracesSampleRate).toBe(1.0);
  });
});
