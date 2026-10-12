// Where a turn is stored when its trial was released while it was being
// answered (companion/runtime.ts, persistTurn).
//
// A fake pool answers by SQL text, so the statements and their parameters are
// what is checked here. What Postgres does with them, the lock included, is
// run for real in money-path.realdb.test.ts ("a turn whose answer straddles
// the payment ...").

import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';

vi.mock('../../../src/companion/claude', () => ({
  callCompanion: vi.fn(),
  DEFAULT_MODEL: 'test-model',
}));
vi.mock('../../../src/companion/memory_capture', () => ({
  captureMemory: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('../../../src/services/email', () => ({ sendCrisisAlertEmail: vi.fn().mockResolvedValue(undefined) }));

import { turn } from '../../../src/companion/runtime';
import { PROMOTED_KEEPER_OF_TRIAL_SQL } from '../../../src/services/keeper_promotion';

const TRIAL = '11111111-1111-4111-8111-111111111111';
const KEEPER = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
// The input rail answers this itself, so no model is involved.
const RAIL_TEXT = 'please write me some testimonials';

type Answer = { rows: unknown[] };

function makePool(rules: Array<[RegExp, Answer]>) {
  const statements: Array<{ sql: string; params: unknown[] }> = [];
  const query = vi.fn(async (sql: unknown, params?: unknown) => {
    const text = String(sql);
    statements.push({ sql: text, params: Array.isArray(params) ? params : [] });
    const rule = rules.find(([pattern]) => pattern.test(text));
    return rule ? rule[1] : { rows: [] };
  });
  return { pool: { query } as unknown as Pool, statements };
}

const ACTIVE_TRIAL: [RegExp, Answer] = [
  /FROM trial_keepers\s+WHERE trial_id = \$1 AND released_at IS NULL/,
  { rows: [{ companion_name: 'Haven', started_at: new Date() }] },
];
const NO_SPEND: [RegExp, Answer] = [/FROM companion_cost_log/, { rows: [{ total: 0, global_total: 0 }] }];
const TRIAL_INSERT = /INSERT INTO companion_turns[\s\S]*FROM trial_keepers t[\s\S]*released_at IS NULL[\s\S]*FOR SHARE OF t/;
const KEEPER_INSERT = /INSERT INTO companion_turns[\s\S]*VALUES \(\$1, \$2, 'table', \$3, \$4, \$5\)/;
const KEEPER_LOOKUP = /FROM trial_keepers t\s+JOIN builds b ON b\.id = t\.handoff_build_id\s+JOIN keepers k/;

describe('persistTurn: the turn is stored under whoever owns the conversation when it is stored', () => {
  it('an active trial: one insert, through the trial row while it is unreleased', async () => {
    const { pool, statements } = makePool([
      ACTIVE_TRIAL,
      NO_SPEND,
      [TRIAL_INSERT, { rows: [{ turn_id: 'turn-trial' }] }],
    ]);
    const result = await turn({ pool, trialId: TRIAL, keeperText: RAIL_TEXT });
    expect(result.turnId).toBe('turn-trial');
    const inserts = statements.filter((s) => /INSERT INTO companion_turns/.test(s.sql));
    expect(inserts).toHaveLength(1);
    expect(inserts[0].sql).toMatch(TRIAL_INSERT);
    expect(inserts[0].params.slice(0, 3)).toEqual([null, TRIAL, RAIL_TEXT]);
    expect(statements.some((s) => KEEPER_LOOKUP.test(s.sql))).toBe(false);
  });

  it('a trial released while the turn was answered: nothing is stored under it, and the turn goes to the keeper it was promoted into', async () => {
    const { pool, statements } = makePool([
      ACTIVE_TRIAL, // the trial was active when the turn began
      NO_SPEND,
      [TRIAL_INSERT, { rows: [] }], // and released by the time the turn is stored
      [KEEPER_LOOKUP, { rows: [{ customer_id: KEEPER }] }],
      [KEEPER_INSERT, { rows: [{ turn_id: 'turn-keeper' }] }],
    ]);
    const result = await turn({ pool, trialId: TRIAL, keeperText: RAIL_TEXT });
    expect(result.turnId).toBe('turn-keeper');
    const lookup = statements.find((s) => KEEPER_LOOKUP.test(s.sql));
    expect(lookup!.sql).toMatch(/t\.trial_id = \$1 AND t\.released_at IS NOT NULL/);
    // The keeper the trial was promoted into: the same statement the cookie middleware asks.
    expect(lookup!.sql).toBe(PROMOTED_KEEPER_OF_TRIAL_SQL);
    expect(lookup!.params).toEqual([TRIAL]);
    const stored = statements.filter((s) => KEEPER_INSERT.test(s.sql));
    expect(stored).toHaveLength(1);
    expect(stored[0].params.slice(0, 3)).toEqual([KEEPER, null, RAIL_TEXT]);
  });

  it('a released trial with no keeper behind it: the turn fails instead of being stored where nothing reads it', async () => {
    const { pool, statements } = makePool([
      ACTIVE_TRIAL,
      NO_SPEND,
      [TRIAL_INSERT, { rows: [] }],
      [KEEPER_LOOKUP, { rows: [] }],
    ]);
    await expect(turn({ pool, trialId: TRIAL, keeperText: RAIL_TEXT })).rejects.toThrow(/no longer active and no keeper holds its conversation/);
    expect(statements.some((s) => KEEPER_INSERT.test(s.sql))).toBe(false);
  });

  it('a keeper: the plain insert, and the trial statements are not used', async () => {
    const { pool, statements } = makePool([
      [/FROM keepers\s+WHERE customer_id = \$1/, { rows: [{ companion_name: 'Haven', started_at: new Date() }] }],
      NO_SPEND,
      [KEEPER_INSERT, { rows: [{ turn_id: 'turn-k' }] }],
    ]);
    const result = await turn({ pool, keeperId: KEEPER, keeperText: RAIL_TEXT });
    expect(result.turnId).toBe('turn-k');
    const inserts = statements.filter((s) => /INSERT INTO companion_turns/.test(s.sql));
    expect(inserts).toHaveLength(1);
    expect(inserts[0].params.slice(0, 2)).toEqual([KEEPER, null]);
    expect(statements.some((s) => /trial_keepers/.test(s.sql))).toBe(false);
  });
});
