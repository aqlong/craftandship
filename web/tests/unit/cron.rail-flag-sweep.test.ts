import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import { runRailFlagSweep } from '../../src/cron/rail_flag_sweep';

const TRIAL_A = '11111111-2222-3333-4444-555555555555';
const TRIAL_B = '22222222-3333-4444-5555-666666666666';
const KEEPER_A = '33333333-4444-5555-6666-777777777777';

function makePool(queryFn: ReturnType<typeof vi.fn>): Pool {
  return { query: queryFn } as unknown as Pool;
}

/**
 * Wire a fake pool so the first query (per-scope SQL) returns `perScope`,
 * the second query (population SQL) returns `population`, and any subsequent
 * agent_learnings UPSERT calls return rowCount=1.
 */
function makeAggPool(
  perScope: Array<{
    keeper_id: string | null;
    trial_id: string | null;
    flag: string;
    turns_with_flag: string;
    total_turns: string;
  }>,
  population: Array<{
    flag: string;
    turns_with_flag: string;
    total_turns: string;
  }>
): { pool: Pool; queryFn: ReturnType<typeof vi.fn> } {
  const queryFn = vi.fn();
  queryFn.mockResolvedValueOnce({ rows: perScope });
  queryFn.mockResolvedValueOnce({ rows: population });
  // All subsequent calls (the UPSERTs) return rowCount=1.
  queryFn.mockResolvedValue({ rows: [], rowCount: 1 });
  return { pool: makePool(queryFn), queryFn };
}

describe('runRailFlagSweep', () => {
  it('returns zero findings when there are no rail flags in the window', async () => {
    const { pool } = makeAggPool([], []);
    const result = await runRailFlagSweep({ pool });
    expect(result.windowDays).toBe(7);
    expect(result.findings).toEqual([]);
    expect(result.findingsRecorded).toBe(0);
  });

  it('selection SQL filters created_at >= NOW() - INTERVAL N days and uses minTurns guard', async () => {
    const { pool, queryFn } = makeAggPool([], []);
    await runRailFlagSweep({ pool });
    const perScopeSql = queryFn.mock.calls[0][0] as string;
    const populationSql = queryFn.mock.calls[1][0] as string;
    expect(perScopeSql).toMatch(/companion_turns/);
    expect(perScopeSql).toMatch(/UNNEST\(rail_flags\)/i);
    expect(perScopeSql).toMatch(/INTERVAL '7 days'/);
    expect(perScopeSql).toMatch(/total_turns >= \$1/);
    expect(populationSql).toMatch(/INTERVAL '7 days'/);
    // Both queries get the minTurns param.
    expect(queryFn.mock.calls[0][1]).toEqual([5]);
    expect(queryFn.mock.calls[1][1]).toEqual([5]);
  });

  it('windowDays opt expands the INTERVAL window in both queries', async () => {
    const { pool, queryFn } = makeAggPool([], []);
    await runRailFlagSweep({ pool, windowDays: 30 });
    expect(queryFn.mock.calls[0][0]).toMatch(/INTERVAL '30 days'/);
    expect(queryFn.mock.calls[1][0]).toMatch(/INTERVAL '30 days'/);
  });

  it('records a finding when a per-trial flag rate exceeds 15% across >=5 turns', async () => {
    const { pool, queryFn } = makeAggPool(
      [
        {
          keeper_id: null,
          trial_id: TRIAL_A,
          flag: 'output-uses-em-dash',
          turns_with_flag: '2',
          total_turns: '10',
        },
      ],
      []
    );
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toHaveLength(1);
    const finding = result.findings[0];
    expect(finding.scope).toBe('trial');
    expect(finding.trialId).toBe(TRIAL_A);
    expect(finding.flag).toBe('output-uses-em-dash');
    expect(finding.flaggedTurns).toBe(2);
    expect(finding.totalTurns).toBe(10);
    expect(finding.flagRate).toBeCloseTo(0.2, 5);
    expect(finding.guardrail).toMatch(/em dashes/i);
    expect(finding.learning).toMatch(/output-uses-em-dash/);
    expect(finding.learning).toMatch(/20\.0%/);
    expect(result.findingsRecorded).toBe(1);

    // Verify an agent_learnings UPSERT fired.
    const upsertCall = queryFn.mock.calls.find((c) =>
      String(c[0]).includes('INSERT INTO agent_learnings')
    );
    expect(upsertCall).toBeDefined();
    expect(upsertCall![0]).toMatch(/ON CONFLICT \(source_agent, category, learning_hash\)/);
    expect(upsertCall![1][0]).toBe('haven_rail_sweep');
    expect(upsertCall![1][1]).toBe('persona_guardrail');
  });

  it('does NOT record a finding when total_turns < minTurns', async () => {
    const { pool } = makeAggPool(
      [
        {
          keeper_id: null,
          trial_id: TRIAL_A,
          flag: 'roleplay-request',
          turns_with_flag: '2',
          total_turns: '4',
        },
      ],
      []
    );
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toEqual([]);
    expect(result.findingsRecorded).toBe(0);
  });

  it('does NOT record a finding when flag rate is exactly at the threshold (strictly greater required)', async () => {
    const { pool } = makeAggPool(
      [
        {
          keeper_id: null,
          trial_id: TRIAL_A,
          flag: 'roleplay-request',
          turns_with_flag: '3',
          total_turns: '20',
        },
      ],
      []
    );
    // 3/20 = 0.15 exactly; not strictly greater than 0.15.
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toEqual([]);
  });

  it('records a population-scope finding when a flag is firing across the cohort', async () => {
    const { pool } = makeAggPool(
      [],
      [
        {
          flag: 'output-claims-personhood',
          turns_with_flag: '6',
          total_turns: '20',
        },
      ]
    );
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toHaveLength(1);
    expect(result.findings[0].scope).toBe('population');
    expect(result.findings[0].keeperId).toBeNull();
    expect(result.findings[0].trialId).toBeNull();
    expect(result.findings[0].flag).toBe('output-claims-personhood');
    expect(result.findings[0].guardrail).toMatch(/personhood/i);
    expect(result.populationFindings).toBe(1);
    expect(result.perScopeFindings).toBe(0);
  });

  it('emits keeper-scope finding for keeper_id rows + trial-scope for trial_id rows', async () => {
    const { pool } = makeAggPool(
      [
        {
          keeper_id: KEEPER_A,
          trial_id: null,
          flag: 'therapy-request',
          turns_with_flag: '3',
          total_turns: '8',
        },
        {
          keeper_id: null,
          trial_id: TRIAL_B,
          flag: 'therapy-request',
          turns_with_flag: '2',
          total_turns: '10',
        },
      ],
      []
    );
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toHaveLength(2);
    const keeperFinding = result.findings.find((f) => f.scope === 'keeper');
    const trialFinding = result.findings.find((f) => f.scope === 'trial');
    expect(keeperFinding?.keeperId).toBe(KEEPER_A);
    expect(keeperFinding?.trialId).toBeNull();
    expect(trialFinding?.trialId).toBe(TRIAL_B);
    expect(trialFinding?.keeperId).toBeNull();
  });

  it('sorts findings: population first, then per-scope by descending flag rate', async () => {
    const { pool } = makeAggPool(
      [
        {
          keeper_id: null,
          trial_id: TRIAL_A,
          flag: 'output-uses-em-dash',
          turns_with_flag: '2',
          total_turns: '10',
        }, // 20%
        {
          keeper_id: KEEPER_A,
          trial_id: null,
          flag: 'roleplay-request',
          turns_with_flag: '5',
          total_turns: '10',
        }, // 50%
      ],
      [
        {
          flag: 'output-uses-em-dash',
          turns_with_flag: '4',
          total_turns: '20',
        }, // 20%
      ]
    );
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toHaveLength(3);
    expect(result.findings[0].scope).toBe('population');
    expect(result.findings[1].scope).toBe('keeper');
    expect(result.findings[1].flagRate).toBeCloseTo(0.5, 5);
    expect(result.findings[2].scope).toBe('trial');
    expect(result.findings[2].flagRate).toBeCloseTo(0.2, 5);
  });

  it('caps recorded findings at maxFindings', async () => {
    const perScope = Array.from({ length: 10 }, (_, i) => ({
      keeper_id: null,
      trial_id: TRIAL_A,
      flag: `output-uses-em-dash`,
      turns_with_flag: '5',
      total_turns: `${10 + i}`,
    }));
    const { pool } = makeAggPool(perScope, []);
    const result = await runRailFlagSweep({ pool, maxFindings: 3 });
    expect(result.findings).toHaveLength(3);
    expect(result.findingsRecorded).toBe(3);
  });

  it('falls back to a generic guardrail for unknown flag types but still records', async () => {
    const { pool } = makeAggPool(
      [
        {
          keeper_id: null,
          trial_id: TRIAL_A,
          flag: 'some-future-flag',
          turns_with_flag: '3',
          total_turns: '10',
        },
      ],
      []
    );
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toHaveLength(1);
    expect(result.findings[0].guardrail).toMatch(/Unrecognized rail flag/);
    expect(result.findingsRecorded).toBe(1);
  });

  it('UPSERT learning_hash is stable across runs for the same scope+flag tuple', async () => {
    // Same finding should hit the same hash; we capture the hashes from
    // two consecutive runs and assert they match.
    const perScope = [
      {
        keeper_id: null,
        trial_id: TRIAL_A,
        flag: 'output-uses-em-dash',
        turns_with_flag: '2',
        total_turns: '10',
      },
    ];

    const queryFn1 = vi.fn();
    queryFn1.mockResolvedValueOnce({ rows: perScope });
    queryFn1.mockResolvedValueOnce({ rows: [] });
    queryFn1.mockResolvedValue({ rows: [], rowCount: 1 });
    const pool1 = makePool(queryFn1);
    await runRailFlagSweep({ pool: pool1 });

    const queryFn2 = vi.fn();
    queryFn2.mockResolvedValueOnce({ rows: perScope });
    queryFn2.mockResolvedValueOnce({ rows: [] });
    queryFn2.mockResolvedValue({ rows: [], rowCount: 1 });
    const pool2 = makePool(queryFn2);
    await runRailFlagSweep({ pool: pool2 });

    const upsert1 = queryFn1.mock.calls.find((c) =>
      String(c[0]).includes('INSERT INTO agent_learnings')
    )!;
    const upsert2 = queryFn2.mock.calls.find((c) =>
      String(c[0]).includes('INSERT INTO agent_learnings')
    )!;
    const hash1 = upsert1[1][3] as string;
    const hash2 = upsert2[1][3] as string;
    expect(hash1).toBe(hash2);
    expect(hash1).toHaveLength(16);
  });

  it('UPSERT learning_hash differs across distinct scope+flag tuples', async () => {
    const { pool, queryFn } = makeAggPool(
      [
        {
          keeper_id: null,
          trial_id: TRIAL_A,
          flag: 'output-uses-em-dash',
          turns_with_flag: '2',
          total_turns: '10',
        },
        {
          keeper_id: null,
          trial_id: TRIAL_B,
          flag: 'output-uses-em-dash',
          turns_with_flag: '2',
          total_turns: '10',
        },
      ],
      [
        {
          flag: 'output-uses-em-dash',
          turns_with_flag: '4',
          total_turns: '20',
        },
      ]
    );
    await runRailFlagSweep({ pool });
    const upserts = queryFn.mock.calls.filter((c) =>
      String(c[0]).includes('INSERT INTO agent_learnings')
    );
    expect(upserts).toHaveLength(3);
    const hashes = upserts.map((c) => c[1][3]);
    expect(new Set(hashes).size).toBe(3); // All distinct.
  });

  it('rejects non-numeric windowDays / minTurns / flagRateThreshold and falls back to defaults (SQL injection hardening)', async () => {
    const { pool, queryFn } = makeAggPool([], []);
    await runRailFlagSweep({
      pool,
      windowDays: 'evil; DROP TABLE companion_turns;' as unknown as number,
      minTurns: 'OR 1=1' as unknown as number,
      flagRateThreshold: 'not-a-number' as unknown as number,
    });
    const sql = queryFn.mock.calls[0][0] as string;
    const params = queryFn.mock.calls[0][1] as unknown[];
    expect(sql).toMatch(/INTERVAL '7 days'/);
    expect(sql).not.toContain('DROP');
    expect(sql).not.toContain('OR 1=1');
    expect(params[0]).toBe(5);
  });

  it('honors the now opt for deterministic windowing in tests', async () => {
    const { pool, queryFn } = makeAggPool([], []);
    const now = new Date('2026-05-04T12:00:00.000Z');
    await runRailFlagSweep({ pool, now });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toContain("TIMESTAMP '2026-05-04T12:00:00.000Z'");
  });

  it('crisis flags get a dedicated guardrail entry pointing at crisis_detection.ts', async () => {
    const { pool } = makeAggPool(
      [],
      [
        {
          flag: 'suicide',
          turns_with_flag: '2',
          total_turns: '10',
        },
      ]
    );
    const result = await runRailFlagSweep({ pool });
    expect(result.findings).toHaveLength(1);
    expect(result.findings[0].guardrail).toMatch(/crisis_detection/);
  });

  it('learning text contains the percentage, raw counts, and window days', async () => {
    const { pool } = makeAggPool(
      [],
      [
        {
          flag: 'output-uses-em-dash',
          turns_with_flag: '7',
          total_turns: '40',
        },
      ]
    );
    const result = await runRailFlagSweep({ pool, windowDays: 7 });
    expect(result.findings[0].learning).toMatch(/17\.5%/);
    expect(result.findings[0].learning).toMatch(/7\/40/);
    expect(result.findings[0].learning).toMatch(/last 7 days/);
    expect(result.findings[0].learning).toMatch(/scope population/);
  });
});
