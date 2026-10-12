import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import { runMemoryLowUtilitySweep } from '../../src/cron/memory_low_utility_sweep';

function makePool(queryFn: ReturnType<typeof vi.fn>): Pool {
  return { query: queryFn } as unknown as Pool;
}

/**
 * Wire a fake pool so the first call (UPDATE...RETURNING) returns
 * `flagged` and the second call (eligible-scope count) returns `scopes`.
 */
function makeSweepPool(opts: {
  flaggedIds?: string[];
  eligibleScopes?: number;
}): { pool: Pool; queryFn: ReturnType<typeof vi.fn> } {
  const queryFn = vi.fn();
  const flaggedIds = opts.flaggedIds ?? [];
  queryFn.mockResolvedValueOnce({
    rows: flaggedIds.map((memory_id) => ({ memory_id })),
    rowCount: flaggedIds.length,
  });
  queryFn.mockResolvedValueOnce({
    rows: [{ scopes: String(opts.eligibleScopes ?? 0) }],
  });
  return { pool: makePool(queryFn), queryFn };
}

describe('runMemoryLowUtilitySweep', () => {
  it('returns zero rowsFlagged when nothing matches', async () => {
    const { pool } = makeSweepPool({ flaggedIds: [], eligibleScopes: 0 });
    const result = await runMemoryLowUtilitySweep({ pool });
    expect(result.staleDays).toBe(14);
    expect(result.minMemoryRows).toBe(20);
    expect(result.rowsFlagged).toBe(0);
    expect(result.eligibleScopes).toBe(0);
  });

  it('issues an UPDATE...RETURNING that filters last_retrieved_at IS NULL + low_utility=FALSE + age', async () => {
    const { pool, queryFn } = makeSweepPool({});
    await runMemoryLowUtilitySweep({ pool });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toMatch(/UPDATE keeper_memory/);
    expect(sql).toMatch(/SET low_utility = TRUE/);
    expect(sql).toMatch(/last_retrieved_at IS NULL/);
    expect(sql).toMatch(/low_utility = FALSE/);
    expect(sql).toMatch(/INTERVAL '14 days'/);
    expect(sql).toMatch(/RETURNING memory_id/);
    // Eligibility CTE uses the >= threshold from $1.
    expect(sql).toMatch(/n >= \$1/);
    // LIMIT cap binds $2.
    expect(sql).toMatch(/LIMIT \$2/);
    expect(queryFn.mock.calls[0][1]).toEqual([20, 5000]);
  });

  it('staleDays opt expands the INTERVAL window', async () => {
    const { pool, queryFn } = makeSweepPool({});
    await runMemoryLowUtilitySweep({ pool, staleDays: 30 });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toMatch(/INTERVAL '30 days'/);
    const result_min = (queryFn.mock.calls[0][1] as unknown[])[0];
    expect(result_min).toBe(20);
  });

  it('minMemoryRows opt threads through to both the UPDATE and the eligibility count', async () => {
    const { pool, queryFn } = makeSweepPool({});
    await runMemoryLowUtilitySweep({ pool, minMemoryRows: 50 });
    expect(queryFn.mock.calls[0][1]).toEqual([50, 5000]);
    // The follow-up SELECT uses the same threshold.
    expect(queryFn.mock.calls[1][1]).toEqual([50]);
  });

  it('maxFlagged caps the per-run flag budget', async () => {
    const { pool, queryFn } = makeSweepPool({});
    await runMemoryLowUtilitySweep({ pool, maxFlagged: 100 });
    expect(queryFn.mock.calls[0][1]).toEqual([20, 100]);
  });

  it('honors `now` override so windows are deterministic in tests', async () => {
    const { pool, queryFn } = makeSweepPool({});
    await runMemoryLowUtilitySweep({
      pool,
      now: new Date('2026-05-04T00:00:00.000Z'),
    });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toMatch(/TIMESTAMP '2026-05-04T00:00:00\.000Z'/);
  });

  it('rejects invalid opts and falls back to defaults', async () => {
    const { pool, queryFn } = makeSweepPool({});
    await runMemoryLowUtilitySweep({
      pool,
      // Negative + zero + non-finite all coerce to defaults.
      minMemoryRows: -1,
      staleDays: 0,
      maxFlagged: Number.NaN,
    });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toMatch(/INTERVAL '14 days'/);
    expect(queryFn.mock.calls[0][1]).toEqual([20, 5000]);
  });

  it('reports rowsFlagged + eligibleScopes from the actual query results', async () => {
    const { pool } = makeSweepPool({
      flaggedIds: ['m-1', 'm-2', 'm-3'],
      eligibleScopes: 7,
    });
    const result = await runMemoryLowUtilitySweep({ pool });
    expect(result.rowsFlagged).toBe(3);
    expect(result.eligibleScopes).toBe(7);
  });
});
