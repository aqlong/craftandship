import { describe, it, expect, vi, beforeEach } from 'vitest';
import { emitEvent, track } from '../../src/services/funnel-events';

function mockReq(overrides: Record<string, unknown> = {}): any {
  return {
    headers: { 'user-agent': 'Mozilla/5.0 (Macintosh) Chrome/120 Safari/537.36' },
    originalUrl: '/lighthouse/table',
    path: '/lighthouse/table',
    harborTrial: { trial_id: 'trial-abc' },
    ...overrides,
  };
}

describe('emitEvent', () => {
  let pool: any;
  beforeEach(() => {
    pool = { query: vi.fn().mockResolvedValue({ rows: [] }) };
  });

  it('inserts a row with name, trial_id, isBot=false for a real browser', async () => {
    const ok = await emitEvent(pool, { name: 'table_view', req: mockReq() });
    expect(ok).toBe(true);
    expect(pool.query).toHaveBeenCalledTimes(1);
    const [sql, params] = pool.query.mock.calls[0];
    expect(sql).toMatch(/INSERT INTO funnel_events/);
    expect(params[0]).toBe('table_view');
    expect(params[1]).toBe('trial-abc'); // trial_id from req.harborTrial
    expect(params[2]).toBe(false); // is_bot \u2014 real browser
    expect(params[3]).toMatch(/^[0-9a-f]{16}$/); // ua_hash, not raw UA
    expect(params[4]).toBe('/lighthouse/table'); // path
  });

  it('classifies a bot UA as is_bot=true', async () => {
    await emitEvent(pool, {
      name: 'table_view',
      req: mockReq({ headers: { 'user-agent': 'facebookexternalhit/1.1' } }),
    });
    const params = pool.query.mock.calls[0][1];
    expect(params[2]).toBe(true);
  });

  it('explicit trialId overrides req.harborTrial', async () => {
    await emitEvent(pool, { name: 'made_public', req: mockReq(), trialId: 'explicit-id' });
    expect(pool.query.mock.calls[0][1][1]).toBe('explicit-id');
  });

  it('serializes props to JSON', async () => {
    await emitEvent(pool, { name: 'build_dispatched', req: mockReq(), props: { build_id: 'b1', turn_count: 7 } });
    const params = pool.query.mock.calls[0][1];
    expect(JSON.parse(params[6])).toEqual({ build_id: 'b1', turn_count: 7 });
  });

  it('works with no req (marketing-page middleware may pass only name)', async () => {
    const ok = await emitEvent(pool, { name: 'landing_view' });
    expect(ok).toBe(true);
    const params = pool.query.mock.calls[0][1];
    expect(params[1]).toBeNull(); // no trial
    expect(params[2]).toBe(true); // no UA -> treated as bot
  });

  it('NEVER throws and returns false when the DB errors (fire-and-forget)', async () => {
    pool.query.mockRejectedValueOnce(new Error('db down'));
    const ok = await emitEvent(pool, { name: 'table_view', req: mockReq() });
    expect(ok).toBe(false); // swallowed, not thrown
  });

  it('truncates overlong path + referrer to column limits', async () => {
    const longPath = '/x'.repeat(400);
    const longRef = 'https://e.example/' + 'y'.repeat(800);
    await emitEvent(pool, {
      name: 'landing_view',
      req: mockReq({ originalUrl: longPath, headers: { 'user-agent': 'Chrome', referer: longRef } }),
    });
    const params = pool.query.mock.calls[0][1];
    expect(params[4].length).toBeLessThanOrEqual(255);
    expect(params[5].length).toBeLessThanOrEqual(500);
  });
});

describe('track (non-awaiting helper)', () => {
  it('does not throw synchronously even if the pool rejects', () => {
    const pool: any = { query: vi.fn().mockRejectedValue(new Error('boom')) };
    expect(() => track(pool, { name: 'table_view' })).not.toThrow();
  });
});
