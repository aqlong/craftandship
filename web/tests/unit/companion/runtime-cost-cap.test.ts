import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  getCompanionDailyCostMicros,
  getCompanionDailyCosts,
  getCompanionGlobalDailyCostCapMicros,
  COMPANION_GLOBAL_DAILY_COST_CAP_DEFAULT_MICROS,
  COMPANION_DAILY_COST_CAP_DEFAULT_MICROS,
  getCompanionDailyCostCapMicros,
  CompanionDailyCostCapError,
} from '../../../src/companion/runtime';
import type { Pool } from 'pg';

/**
 * Tests for the per-trial / per-keeper daily cost cap helpers
 * (harbor-companion-turn-rate-limit-and-cost-cap, 2026-05-09).
 *
 * The cap reads SUM(total_cost_micros) from companion_cost_log
 * scoped to the identity (trial_id or keeper_id) over the last 24h.
 * Default cap is $5/day in micros (5_000_000); env override
 * `CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS`. The default is the same in every
 * environment, the test environment included.
 */

function makeMockPool(rows: Array<{ total: string | number | null }>): Pool {
  return {
    query: vi.fn().mockResolvedValue({ rows }),
  } as unknown as Pool;
}

describe('getCompanionDailyCostCapMicros', () => {
  const ORIGINAL = process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS;
  const ORIGINAL_NODE_ENV = process.env.NODE_ENV;

  afterEach(() => {
    if (ORIGINAL === undefined) delete process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS;
    else process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS = ORIGINAL;
    if (ORIGINAL_NODE_ENV === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = ORIGINAL_NODE_ENV;
  });

  it('defaults to $5 in every environment, the test environment included', () => {
    delete process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS;
    for (const nodeEnv of ['test', 'development', 'production', 'staging']) {
      process.env.NODE_ENV = nodeEnv;
      expect(getCompanionDailyCostCapMicros()).toBe(COMPANION_DAILY_COST_CAP_DEFAULT_MICROS);
    }
    delete process.env.NODE_ENV;
    expect(getCompanionDailyCostCapMicros()).toBe(COMPANION_DAILY_COST_CAP_DEFAULT_MICROS);
    expect(COMPANION_DAILY_COST_CAP_DEFAULT_MICROS).toBe(5_000_000);
  });

  it('honors env override in test mode', () => {
    process.env.NODE_ENV = 'test';
    process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS = '500000';
    expect(getCompanionDailyCostCapMicros()).toBe(500_000);
  });

  it('rejects non-positive env values + falls back to default', () => {
    process.env.NODE_ENV = 'test';
    process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS = '0';
    expect(getCompanionDailyCostCapMicros()).toBe(COMPANION_DAILY_COST_CAP_DEFAULT_MICROS);
    process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS = 'not-a-number';
    expect(getCompanionDailyCostCapMicros()).toBe(COMPANION_DAILY_COST_CAP_DEFAULT_MICROS);
  });
});

describe('getCompanionDailyCostMicros', () => {
  it('SELECTs SUM scoped to trial_id when trialId is provided', async () => {
    const pool = makeMockPool([{ total: 1_234_567 }]);
    const total = await getCompanionDailyCostMicros(pool, {
      trialId: 'trial-abc',
    });
    expect(total).toBe(1_234_567);
    const calls = (pool.query as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls).toHaveLength(1);
    const sql = String(calls[0][0]);
    expect(sql).toContain('ct.trial_id = $1');
    expect(sql).toContain('SUM(ccl.total_cost_micros)');
    expect(sql).toContain("INTERVAL '24 hours'");
    expect(calls[0][1]).toEqual(['trial-abc']);
  });

  it('SELECTs SUM scoped to keeper_id when keeperId is provided', async () => {
    const pool = makeMockPool([{ total: '500000' }]);
    const total = await getCompanionDailyCostMicros(pool, {
      keeperId: 'keeper-xyz',
    });
    expect(total).toBe(500_000);
    const calls = (pool.query as ReturnType<typeof vi.fn>).mock.calls;
    const sql = String(calls[0][0]);
    expect(sql).toContain('ct.keeper_id = $1');
    expect(calls[0][1]).toEqual(['keeper-xyz']);
  });

  it('returns 0 when neither trialId nor keeperId is provided', async () => {
    const pool = makeMockPool([{ total: 999 }]);
    const total = await getCompanionDailyCostMicros(pool, {});
    expect(total).toBe(0);
    expect((pool.query as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled();
  });

  it('parses pg bigint string return values into numbers', async () => {
    const pool = makeMockPool([{ total: '7777777' }]);
    const total = await getCompanionDailyCostMicros(pool, {
      trialId: 'trial-bigint',
    });
    expect(total).toBe(7_777_777);
  });

  it('fail-open: returns 0 when SUM query throws', async () => {
    const pool = {
      query: vi.fn().mockRejectedValue(new Error('connection refused')),
    } as unknown as Pool;
    const total = await getCompanionDailyCostMicros(pool, {
      trialId: 'trial-blip',
    });
    // Fail-open: a transient DB failure must NOT 429 a real keeper.
    expect(total).toBe(0);
  });

  it('handles an empty result and a NULL SUM (no rows in the last 24h) as 0', async () => {
    // SUM() over no rows is NULL; a query that returns no row at all is also possible.
    expect(await getCompanionDailyCostMicros(makeMockPool([]), { trialId: 'trial-quiet' })).toBe(0);
    expect(await getCompanionDailyCostMicros(makeMockPool([{ total: null }]), { trialId: 'trial-quiet' })).toBe(0);
    expect(await getCompanionDailyCostMicros(makeMockPool([{ total: 0 }]), { trialId: 'trial-quiet' })).toBe(0);
  });
});

describe('CompanionDailyCostCapError', () => {
  it('carries currentMicros + capMicros + a friendly dollar-formatted message', () => {
    const err = new CompanionDailyCostCapError(7_500_000, 5_000_000);
    expect(err.name).toBe('CompanionDailyCostCapError');
    expect(err.currentMicros).toBe(7_500_000);
    expect(err.capMicros).toBe(5_000_000);
    expect(err.message).toMatch(/\$7\.50/);
    expect(err.message).toMatch(/\$5\.00/);
  });

  it('is an Error instance + can be caught with instanceof', () => {
    const err = new CompanionDailyCostCapError(1, 0);
    expect(err instanceof Error).toBe(true);
    expect(err instanceof CompanionDailyCostCapError).toBe(true);
  });
});


describe('shared daily ceiling across every identity', () => {
  const GLOBAL_ENV = 'CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS';
  const saved = { cap: process.env[GLOBAL_ENV], nodeEnv: process.env.NODE_ENV };

  afterEach(() => {
    if (saved.cap === undefined) delete process.env[GLOBAL_ENV];
    else process.env[GLOBAL_ENV] = saved.cap;
    process.env.NODE_ENV = saved.nodeEnv;
  });

  it('defaults to the same finite dollar amount in every environment', () => {
    delete process.env[GLOBAL_ENV];
    for (const nodeEnv of ['production', 'development', 'staging', 'test']) {
      process.env.NODE_ENV = nodeEnv;
      expect(getCompanionGlobalDailyCostCapMicros()).toBe(COMPANION_GLOBAL_DAILY_COST_CAP_DEFAULT_MICROS);
    }
    delete process.env.NODE_ENV;
    expect(getCompanionGlobalDailyCostCapMicros()).toBe(COMPANION_GLOBAL_DAILY_COST_CAP_DEFAULT_MICROS);
    expect(COMPANION_GLOBAL_DAILY_COST_CAP_DEFAULT_MICROS).toBe(50_000_000);
  });

  it('honours the env override and ignores junk or non-positive values', () => {
    process.env.NODE_ENV = 'production';
    process.env[GLOBAL_ENV] = '7500000';
    expect(getCompanionGlobalDailyCostCapMicros()).toBe(7_500_000);
    for (const junk of ['0', '-1', 'lots', '']) {
      process.env[GLOBAL_ENV] = junk;
      expect(getCompanionGlobalDailyCostCapMicros()).toBe(COMPANION_GLOBAL_DAILY_COST_CAP_DEFAULT_MICROS);
    }
  });

  it('reads this identity and everyone in one query', async () => {
    const pool = {
      query: vi.fn().mockResolvedValue({ rows: [{ total: '120', global_total: '98765' }] }),
    } as unknown as Pool;
    const costs = await getCompanionDailyCosts(pool, { trialId: 'trial-abc' });
    expect(costs).toEqual({ identityMicros: 120, globalMicros: 98_765 });
    const calls = (pool.query as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls).toHaveLength(1);
    const sql = String(calls[0][0]);
    expect(sql).toContain('FILTER (WHERE ct.trial_id = $1)');
    expect(sql).toContain('AS global_total');
    expect(sql).toContain("INTERVAL '24 hours'");
  });

  it('fails open on a database error (both figures 0)', async () => {
    const pool = { query: vi.fn().mockRejectedValue(new Error('down')) } as unknown as Pool;
    vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(await getCompanionDailyCosts(pool, { keeperId: 'k' })).toEqual({ identityMicros: 0, globalMicros: 0 });
    vi.restoreAllMocks();
  });

  it('the error records which cap refused the turn', () => {
    expect(new CompanionDailyCostCapError(1, 1).scope).toBe('identity');
    expect(new CompanionDailyCostCapError(1, 1, 'shared').scope).toBe('shared');
  });
});
