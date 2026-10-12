// Whose notes: the owner key of keeper_memory, for reading and for writing.
//
// A row is keyed by a trial id or by a keeper id, never both, and payment
// moves a trial's rows to the keeper. These tests pin the two places that
// choose the key: the owner's own list (the notes panel and the recall page)
// and the statement memory capture inserts with. What Postgres does with them
// is exercised in money-path.realdb.test.ts.

import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import { listMemoryNotes, memoryOwnerColumn } from '../../../src/companion/memory_retrieval';
import { storeCapturedItems } from '../../../src/companion/memory_capture';

function makePool(answers: Array<{ rows: unknown[] }>) {
  const query = vi.fn();
  for (const answer of answers) query.mockResolvedValueOnce(answer);
  query.mockResolvedValue({ rows: [] });
  return { pool: { query } as unknown as Pool, query };
}

describe('memoryOwnerColumn', () => {
  it('names the one column an owner is stored under', () => {
    expect(memoryOwnerColumn({ keeperId: 'k-1' })).toEqual({ column: 'keeper_id', value: 'k-1' });
    expect(memoryOwnerColumn({ trialId: 't-1' })).toEqual({ column: 'trial_id', value: 't-1' });
  });
});

describe('listMemoryNotes: the owner reading their own notes', () => {
  it('asks by keeper id for a keeper and by trial id for a trial, never by both', async () => {
    const keeper = makePool([{ rows: [] }]);
    await listMemoryNotes(keeper.pool, { keeperId: 'k-1' }, 25);
    expect(String(keeper.query.mock.calls[0][0])).toMatch(/WHERE keeper_id = \$1 AND deleted_at IS NULL/);
    expect(String(keeper.query.mock.calls[0][0])).not.toMatch(/trial_id/);
    expect(keeper.query.mock.calls[0][1]).toEqual(['k-1', 25]);

    const trial = makePool([{ rows: [] }]);
    await listMemoryNotes(trial.pool, { trialId: 't-1' }, 200);
    expect(String(trial.query.mock.calls[0][0])).toMatch(/WHERE trial_id = \$1 AND deleted_at IS NULL/);
    expect(String(trial.query.mock.calls[0][0])).not.toMatch(/keeper_id/);
    expect(trial.query.mock.calls[0][1]).toEqual(['t-1', 200]);
  });

  it('returns the rows newest first, soft-deleted ones left out, and does not filter by partition', async () => {
    const rows = [{ memory_id: 'm-2' }, { memory_id: 'm-1' }];
    const { pool, query } = makePool([{ rows }]);
    expect(await listMemoryNotes(pool, { keeperId: 'k-1' }, 25)).toEqual(rows);
    const sql = String(query.mock.calls[0][0]);
    expect(sql).toMatch(/ORDER BY updated_at DESC\s+LIMIT \$2/);
    expect(sql).not.toMatch(/partition IN/);
  });
});

describe('storeCapturedItems: a note takes the identity its source turn has when it is written', () => {
  const item = { category: 'fact', content: 'The keeper teaches reiki.' };

  it('inserts through the turn row (keeper_id and trial_id copied from it, under a share lock), not from the caller\'s ids', async () => {
    const { pool, query } = makePool([{ rows: [] }, { rows: [{ memory_id: 'm-1' }] }]);
    const result = await storeCapturedItems({
      pool,
      trialId: 't-1',
      sourceTurnId: 'turn-1',
      items: [item],
      embeddings: [null],
    });
    expect(result).toEqual({ insertedMemoryIds: ['m-1'], skippedDuplicateCount: 0 });

    const [sql, params] = query.mock.calls[1];
    expect(String(sql)).toMatch(/INSERT INTO keeper_memory/);
    expect(String(sql)).toMatch(/SELECT ct\.keeper_id, ct\.trial_id, \$2, \$3, \$4, \$5, ct\.turn_id, \$6\s+FROM companion_turns ct\s+WHERE ct\.turn_id = \$1\s+FOR SHARE OF ct/);
    // The caller's trial id is not among the insert's parameters at all.
    expect(params).toEqual(['turn-1', 'working', 'fact', 'The keeper teaches reiki.', null, null]);
    expect(params).not.toContain('t-1');
  });

  it('files a sensitive item in the sacred partition, as before', async () => {
    const { pool, query } = makePool([{ rows: [] }, { rows: [{ memory_id: 'm-1' }] }]);
    await storeCapturedItems({
      pool,
      keeperId: 'k-1',
      sourceTurnId: 'turn-1',
      items: [{ ...item, sacred_likelihood: 0.9 }],
      embeddings: [null],
    });
    expect(query.mock.calls[1][1][1]).toBe('sacred');
  });

  it('skips an item the owner already holds (the dedupe read is by the caller\'s id)', async () => {
    const { pool, query } = makePool([{ rows: [{ memory_id: 'm-0', content: '  the keeper TEACHES reiki. ', embedding: null }] }]);
    const result = await storeCapturedItems({ pool, keeperId: 'k-1', sourceTurnId: 'turn-1', items: [item], embeddings: [null] });
    expect(result).toEqual({ insertedMemoryIds: [], skippedDuplicateCount: 1 });
    expect(String(query.mock.calls[0][0])).toMatch(/WHERE keeper_id = \$1 AND deleted_at IS NULL/);
    expect(query).toHaveBeenCalledTimes(1);
  });

  it('stores nothing, and says so, when the turn no longer exists (its account was deleted)', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const { pool, query } = makePool([{ rows: [] }, { rows: [] }]);
    const result = await storeCapturedItems({
      pool,
      keeperId: 'k-1',
      sourceTurnId: 'turn-gone',
      items: [item, { category: 'fact', content: 'A second note.' }],
      embeddings: [null, null],
    });
    expect(result.insertedMemoryIds).toEqual([]);
    // It stops at the first missing turn instead of asking again for each item.
    expect(query).toHaveBeenCalledTimes(2);
    expect(warn.mock.calls.map((call) => call.join(' ')).join('\n')).toMatch(/source turn %s no longer exists/);
    warn.mockRestore();
  });
});
