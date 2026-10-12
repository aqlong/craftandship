import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Pool } from 'pg';
import {
  runPersonaLearningDigest,
  formatDigestBody,
  PERSONA_DIGEST_CATEGORIES,
} from '../../src/cron/persona_learning_digest';

function makePool(queryFn: ReturnType<typeof vi.fn>): Pool {
  return { query: queryFn } as unknown as Pool;
}

const SAMPLE_ROWS = [
  {
    id: 'aaa',
    source_agent: 'haven_rail_sweep',
    category: 'harbor_persona',
    learning: 'Reinforce no-em-dash rule near top of persona prompt.',
    confidence: 0.7,
    created_at: new Date('2026-04-30T12:00:00Z'),
  },
  {
    id: 'bbb',
    source_agent: 'architect',
    category: 'archetype_correction',
    learning: 'Map "piano lessons" to coach-consultant archetype.',
    confidence: 0.5,
    created_at: new Date('2026-05-01T08:00:00Z'),
  },
  {
    id: 'ccc',
    source_agent: 'backend_engineer',
    category: 'harbor_extraction',
    learning: 'Trial=xyz produced BARREN_SPEC: 0/8 sections handled.',
    confidence: 0.5,
    created_at: new Date('2026-05-02T09:00:00Z'),
  },
];

describe('runPersonaLearningDigest', () => {
  let sendFn: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    sendFn = vi.fn().mockResolvedValue(undefined);
  });

  it('returns no_candidates and does not email when there are zero unapplied learnings', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [] });
    const result = await runPersonaLearningDigest({
      pool: makePool(queryFn),
      sendFn,
    });
    expect(result.surfacedCount).toBe(0);
    expect(result.totalCandidates).toBe(0);
    expect(result.emailed).toBe(false);
    expect(result.skippedReason).toBe('no_candidates');
    expect(sendFn).not.toHaveBeenCalled();
  });

  it('queries agent_learnings filtering on category, last_applied_at IS NULL, and the window', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [] });
    await runPersonaLearningDigest({ pool: makePool(queryFn), sendFn });
    expect(queryFn).toHaveBeenCalledOnce();
    const sql = queryFn.mock.calls[0][0] as string;
    const params = queryFn.mock.calls[0][1] as unknown[];
    expect(sql).toMatch(/agent_learnings/);
    expect(sql).toMatch(/last_applied_at IS NULL/);
    expect(sql).toMatch(/category = ANY/);
    expect(sql).toMatch(/created_at >= NOW\(\) - \(\$2 \|\| ' days'\)::INTERVAL/);
    // Categories arg.
    expect(params[0]).toEqual([...PERSONA_DIGEST_CATEGORIES]);
    // Default window 30 days.
    expect(params[1]).toBe('30');
  });

  it('honors a custom windowDays parameter', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [] });
    await runPersonaLearningDigest({
      pool: makePool(queryFn),
      sendFn,
      windowDays: 14,
    });
    const params = queryFn.mock.calls[0][1] as unknown[];
    expect(params[1]).toBe('14');
  });

  it('sends a digest email with one bullet per learning and counts grouped by category', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: SAMPLE_ROWS });
    const result = await runPersonaLearningDigest({
      pool: makePool(queryFn),
      sendFn,
    });
    expect(result.surfacedCount).toBe(3);
    expect(result.emailed).toBe(true);
    expect(result.byCategory).toEqual({
      harbor_persona: 1,
      archetype_correction: 1,
      harbor_extraction: 1,
    });
    expect(sendFn).toHaveBeenCalledOnce();
    const arg = sendFn.mock.calls[0][0] as { subject: string; body: string };
    expect(arg.subject).toMatch(/CAS Harbor.*persona digest/);
    expect(arg.subject).toMatch(/3 unapplied learnings/);
    // Each learning text should appear in the body.
    for (const row of SAMPLE_ROWS) {
      expect(arg.body).toContain(row.learning);
    }
    // Sectioned by category.
    expect(arg.body).toContain('== harbor_persona (1) ==');
    expect(arg.body).toContain('== archetype_correction (1) ==');
    expect(arg.body).toContain('== harbor_extraction (1) ==');
    // Includes the apply-and-mark hint.
    expect(arg.body).toMatch(/UPDATE agent_learnings SET last_applied_at = NOW/);
  });

  it('singularizes the subject when only one learning is surfaced', async () => {
    const queryFn = vi
      .fn()
      .mockResolvedValue({ rows: [SAMPLE_ROWS[0]] });
    await runPersonaLearningDigest({
      pool: makePool(queryFn),
      sendFn,
    });
    const arg = sendFn.mock.calls[0][0] as { subject: string };
    expect(arg.subject).toMatch(/1 unapplied learning\b/);
    expect(arg.subject).not.toMatch(/learnings/);
  });

  it('caps total surfaced rows at maxTotal even when the SQL returns more', async () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      ...SAMPLE_ROWS[0],
      id: `row-${i}`,
      learning: `Learning #${i}`,
    }));
    const queryFn = vi.fn().mockResolvedValue({ rows: many });
    const result = await runPersonaLearningDigest({
      pool: makePool(queryFn),
      sendFn,
      maxTotal: 5,
    });
    expect(result.surfacedCount).toBe(5);
    const body = (sendFn.mock.calls[0][0] as { body: string }).body;
    expect(body).toContain('Learning #0');
    expect(body).toContain('Learning #4');
    expect(body).not.toContain('Learning #5');
  });

  it('returns send_failed and does not throw when the email send fails', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: SAMPLE_ROWS });
    sendFn.mockRejectedValueOnce(new Error('resend exploded'));
    const result = await runPersonaLearningDigest({
      pool: makePool(queryFn),
      sendFn,
    });
    expect(result.emailed).toBe(false);
    expect(result.skippedReason).toBe('send_failed');
    expect(result.surfacedCount).toBe(3);
  });

  it('coerces non-numeric option values back to defaults', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [] });
    await runPersonaLearningDigest({
      pool: makePool(queryFn),
      sendFn,
      windowDays: -5 as unknown as number,
      maxPerCategory: 0 as unknown as number,
      maxTotal: NaN as unknown as number,
    });
    const params = queryFn.mock.calls[0][1] as unknown[];
    expect(params[1]).toBe('30');
    expect(params[2]).toBe(25);
  });
});

describe('formatDigestBody', () => {
  it('lists every category with its count even when empty', () => {
    const body = formatDigestBody({
      rows: [SAMPLE_ROWS[0]],
      windowDays: 30,
      byCategory: { harbor_persona: 1 },
    });
    expect(body).toContain('  - harbor_persona: 1');
    expect(body).toContain('  - archetype_correction: 0');
    expect(body).toContain('  - harbor_extraction: 0');
  });

  it('includes the source_agent and confidence in each bullet', () => {
    const body = formatDigestBody({
      rows: [SAMPLE_ROWS[0]],
      windowDays: 7,
      byCategory: { harbor_persona: 1 },
    });
    expect(body).toContain('haven_rail_sweep');
    expect(body).toContain('conf=0.70');
    expect(body).toContain('2026-04-30');
  });

  it('collapses internal whitespace inside each learning text', () => {
    const noisy = {
      ...SAMPLE_ROWS[0],
      learning: 'a\n\nb\t\tc   d',
    };
    const body = formatDigestBody({
      rows: [noisy],
      windowDays: 7,
      byCategory: { harbor_persona: 1 },
    });
    expect(body).toContain('a b c d');
  });
});
