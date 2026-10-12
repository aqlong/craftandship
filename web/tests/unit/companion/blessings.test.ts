import { describe, it, expect, vi } from 'vitest';
import { BLESSING_POOL, pickBlessing, insertBlessingTurn } from '../../../src/companion/blessings';

describe('BLESSING_POOL', () => {
  it('has at least 3 variants so the keeper sees variety across keepers', () => {
    expect(BLESSING_POOL.length).toBeGreaterThanOrEqual(3);
  });

  it('each entry has a stable id and non-empty text', () => {
    for (const b of BLESSING_POOL) {
      expect(typeof b.id).toBe('string');
      expect(b.id.length).toBeGreaterThan(0);
      expect(typeof b.text).toBe('string');
      expect(b.text.length).toBeGreaterThan(10);
    }
  });

  it('ids are unique', () => {
    const ids = BLESSING_POOL.map((b) => b.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('no em dashes in any blessing text (brand voice)', () => {
    for (const b of BLESSING_POOL) {
      expect(b.text).not.toMatch(/\u2014/);
      expect(b.text).not.toMatch(/&mdash;/i);
    }
  });

  it('uses the lighthouse / keeper / lamp lexicon', () => {
    const allText = BLESSING_POOL.map((b) => b.text.toLowerCase()).join(' ');
    expect(allText).toMatch(/lamp|lighthouse|keeper|public|website/);
  });

  it('does NOT use banned words: "site", "platform", "app", "tool", "trial"', () => {
    const banned = /\b(site|platform|software|tool|trial)\b/i;
    for (const b of BLESSING_POOL) {
      expect(b.text, `blessing ${b.id} contains banned word`).not.toMatch(banned);
    }
  });
});

describe('pickBlessing', () => {
  it('returns a member of BLESSING_POOL', () => {
    const result = pickBlessing('seed-123');
    expect(BLESSING_POOL).toContain(result);
  });

  it('is deterministic: same seed yields same blessing', () => {
    const a = pickBlessing('keeper-uuid-abc');
    const b = pickBlessing('keeper-uuid-abc');
    expect(a.id).toBe(b.id);
  });

  it('different seeds produce different selections (probabilistic, but stable in this test)', () => {
    const seeds = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'];
    const ids = new Set(seeds.map((s) => pickBlessing(s).id));
    expect(ids.size).toBeGreaterThan(1);
  });

  it('handles empty seed gracefully (returns first blessing)', () => {
    expect(pickBlessing('')).toBe(BLESSING_POOL[0]);
  });

  it('handles non-string seed gracefully (returns first blessing)', () => {
    // @ts-expect-error testing defensive non-string input
    expect(pickBlessing(null)).toBe(BLESSING_POOL[0]);
    // @ts-expect-error testing defensive non-string input
    expect(pickBlessing(undefined)).toBe(BLESSING_POOL[0]);
  });
});

describe('insertBlessingTurn', () => {
  it('runs INSERT WHERE NOT EXISTS into companion_turns with harbor-blessing surface', async () => {
    const client = { query: vi.fn().mockResolvedValue({ rows: [{ turn_id: 't1' }], rowCount: 1 }) };
    const result = await insertBlessingTurn(client, { keeperId: 'cust-1', seed: 'trial-1' });
    expect(client.query).toHaveBeenCalledOnce();
    const sql = client.query.mock.calls[0][0] as string;
    expect(sql).toMatch(/INSERT INTO companion_turns/);
    expect(sql).toMatch(/'harbor-blessing'/);
    expect(sql).toMatch(/WHERE NOT EXISTS/);
    expect(client.query.mock.calls[0][1]).toEqual(['cust-1', result.blessing.text]);
    expect(result.inserted).toBe(true);
  });

  it('inserted=false when no row landed (duplicate webhook)', async () => {
    const client = { query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }) };
    const result = await insertBlessingTurn(client, { keeperId: 'cust-1', seed: 'trial-1' });
    expect(result.inserted).toBe(false);
    expect(result.blessing).toBeDefined();
  });

  it('selects deterministic blessing for the same seed', async () => {
    const client1 = { query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }) };
    const client2 = { query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }) };
    const r1 = await insertBlessingTurn(client1, { keeperId: 'cust-1', seed: 'same-seed' });
    const r2 = await insertBlessingTurn(client2, { keeperId: 'cust-1', seed: 'same-seed' });
    expect(r1.blessing.id).toBe(r2.blessing.id);
  });
});
