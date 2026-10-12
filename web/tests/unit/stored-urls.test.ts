// Two places write a request's URL into a table: the trial mint
// (trial_keepers.entry_path and entry_referrer, middleware/trial-cookie.ts)
// and the funnel events (funnel_events.path and referrer,
// services/funnel-events.ts). Both must store the path only. A /lighthouse
// URL can carry an admin's `dev_trial` id or a payment provider's
// `session_id`, and a Referer carries whatever the previous page had in its
// address. Nothing in the repository reads the query of either column: the
// campaign parameters are extracted into entry_utm, and event details go in
// funnel_events.props.

import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import type { NextFunction, Request, Response } from 'express';
import { trialCookie } from '../../src/middleware/trial-cookie';
import { emitEvent } from '../../src/services/funnel-events';

// Built at run time so no identifier-shaped literal sits in the source.
const DEV_TRIAL = ['9f3c2a10', '5b7d', '4e21', '8a6f', '0c1d2e3f4a5b'].join('-');
const SESSION_ID = ['cs', 'test', 'a1b2c3d4e5f6a7b8'].join('_');
const QUERY = `session_id=${SESSION_ID}&dev_trial=${DEV_TRIAL}&utm_source=mail&utm_medium=email&utm_campaign=spring`;
const BROWSER = 'Mozilla/5.0 (Macintosh) Chrome/120 Safari/537.36';

function requestFor(pathname: string): Request {
  return {
    headers: {
      'user-agent': BROWSER,
      referer: `https://example.com/lighthouse/lamp/build-1/published?${QUERY}#top`,
    },
    originalUrl: `${pathname}?${QUERY}`,
    path: pathname,
    query: {
      session_id: SESSION_ID,
      dev_trial: DEV_TRIAL,
      utm_source: 'mail',
      utm_medium: 'email',
      utm_campaign: 'spring',
    },
    ip: '203.0.113.5',
  } as unknown as Request;
}

describe('the trial mint stores where a visitor arrived, without the query string', () => {
  it('entry_path and entry_referrer hold paths only, and entry_utm still holds the campaign', async () => {
    const minted = {
      trial_id: '11111111-2222-3333-4444-555555555555',
      cookie_token: 'a'.repeat(64),
      companion_name: 'Haven',
      companion_assigned: 'Haven',
      started_at: new Date(),
      expires_at: new Date(Date.now() + 86_400_000),
      last_seen_at: new Date(),
    };
    const query = vi.fn().mockResolvedValue({ rows: [minted] });
    const handler = trialCookie({ query } as unknown as Pool);
    const next = vi.fn();
    await handler(requestFor('/lighthouse/table'), { cookie: vi.fn() } as unknown as Response, next as unknown as NextFunction);

    const insert = query.mock.calls.find((call) => /INSERT INTO trial_keepers/.test(String(call[0])));
    expect(insert).toBeDefined();
    const params = insert![1] as unknown[];
    // (cookie_token, companion, companion, is_synthetic, ua_hash, entry_referrer, entry_path, entry_utm)
    expect(params[5]).toBe('https://example.com/lighthouse/lamp/build-1/published');
    expect(params[6]).toBe('/lighthouse/table');
    expect(params[7]).toBe('mail/email/spring');
    const stored = JSON.stringify(params);
    expect(stored).not.toContain(SESSION_ID);
    expect(stored).not.toContain(DEV_TRIAL);
    expect(next).toHaveBeenCalledWith();
  });
});

describe('a funnel event stores the page it happened on, without the query string', () => {
  it('path and referrer hold paths only', async () => {
    const query = vi.fn().mockResolvedValue({ rows: [] });
    const ok = await emitEvent({ query } as unknown as Pool, { name: 'table_view', req: requestFor('/lighthouse/table') });
    expect(ok).toBe(true);
    const params = query.mock.calls[0][1] as unknown[];
    // (name, trial_id, is_bot, ua_hash, path, referrer, props)
    expect(params[4]).toBe('/lighthouse/table');
    expect(params[5]).toBe('https://example.com/lighthouse/lamp/build-1/published');
    const stored = JSON.stringify(params);
    expect(stored).not.toContain(SESSION_ID);
    expect(stored).not.toContain(DEV_TRIAL);
    expect(stored).not.toContain('utm_');
  });

  it('a request with no referrer and no query is stored as before', async () => {
    const query = vi.fn().mockResolvedValue({ rows: [] });
    await emitEvent({ query } as unknown as Pool, {
      name: 'landing_view',
      req: { headers: { 'user-agent': BROWSER }, originalUrl: '/pricing', path: '/pricing' } as unknown as Request,
    });
    const params = query.mock.calls[0][1] as unknown[];
    expect(params[4]).toBe('/pricing');
    expect(params[5]).toBeNull();
  });
});
