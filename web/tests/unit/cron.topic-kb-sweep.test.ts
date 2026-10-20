import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import {
  runTopicKbSweep,
  buildCorpus,
  parseClustersJson,
  isValidCluster,
  containsLinkOrMarkup,
  normalizeQuestion,
  buildExtractPrompt,
  type ExtractFn,
  type ExtractResult,
  type TopicCluster,
} from '../../src/cron/topic_kb_sweep';

function makePool(queryFn: ReturnType<typeof vi.fn>): Pool {
  return { query: queryFn } as unknown as Pool;
}

function makeExtractFn(result: ExtractResult): ExtractFn {
  return vi.fn().mockResolvedValue(result) as unknown as ExtractFn;
}

function makeTurnRows(n: number, prefix = 'How does pricing work? '): Array<{ keeper_text: string }> {
  return Array.from({ length: n }, (_, i) => ({
    keeper_text: `${prefix}message ${i}`,
  }));
}

describe('runTopicKbSweep', () => {
  it('returns turnsScanned=0 and skips Haiku when no turns in window', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: [] });
    const pool = makePool(queryFn);
    const extractFn = vi.fn();
    const result = await runTopicKbSweep({ pool, extractFn: extractFn as unknown as ExtractFn });
    expect(result.turnsScanned).toBe(0);
    expect(result.clustersExtracted).toBe(0);
    expect(result.insertedCount).toBe(0);
    expect(extractFn).not.toHaveBeenCalled();
  });

  it('skips Haiku when fewer than minTurns rows exist', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(3) });
    const pool = makePool(queryFn);
    const extractFn = vi.fn();
    const result = await runTopicKbSweep({
      pool,
      minTurns: 10,
      extractFn: extractFn as unknown as ExtractFn,
    });
    expect(result.turnsScanned).toBe(3);
    expect(result.clustersExtracted).toBe(0);
    expect(extractFn).not.toHaveBeenCalled();
  });

  it('selection SQL filters keeper_text NOT NULL + 7-day window', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: [] });
    const pool = makePool(queryFn);
    await runTopicKbSweep({
      pool,
      extractFn: makeExtractFn({ clusters: [], costMicros: 0 }),
    });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toMatch(/companion_turns/);
    expect(sql).toMatch(/keeper_text IS NOT NULL/);
    expect(sql).toMatch(/INTERVAL '7 days'/);
    expect(sql).toMatch(/ORDER BY created_at ASC/);
  });

  it('windowDays opt expands the INTERVAL', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: [] });
    const pool = makePool(queryFn);
    await runTopicKbSweep({
      pool,
      windowDays: 30,
      extractFn: makeExtractFn({ clusters: [], costMicros: 0 }),
    });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toMatch(/INTERVAL '30 days'/);
  });

  it('honors the now opt for deterministic windowing', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: [] });
    const pool = makePool(queryFn);
    const now = new Date('2026-05-04T12:00:00.000Z');
    await runTopicKbSweep({
      pool,
      now,
      extractFn: makeExtractFn({ clusters: [], costMicros: 0 }),
    });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toContain("TIMESTAMP '2026-05-04T12:00:00.000Z'");
  });

  it('rejects non-numeric windowDays / minTurns and falls back to defaults (SQL injection hardening)', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: [] });
    const pool = makePool(queryFn);
    await runTopicKbSweep({
      pool,
      windowDays: 'evil; DROP TABLE companion_turns;' as unknown as number,
      minTurns: 'OR 1=1' as unknown as number,
      extractFn: makeExtractFn({ clusters: [], costMicros: 0 }),
    });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toMatch(/INTERVAL '7 days'/);
    expect(sql).not.toContain('DROP');
    expect(sql).not.toContain('OR 1=1');
  });

  it('extracts and inserts new Q&A pairs when turns exceed minTurns', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(15) });
    // Two INSERTs (one per cluster), each succeeds with rowCount=1.
    queryFn.mockResolvedValueOnce({ rowCount: 1, rows: [] });
    queryFn.mockResolvedValueOnce({ rowCount: 1, rows: [] });
    const pool = makePool(queryFn);
    const extractFn = makeExtractFn({
      clusters: [
        { question: 'How does pricing work?', answer: 'You pick any monthly amount between $19 and $39.' },
        { question: 'Can I cancel anytime?', answer: 'Yes; your site stays up if you pause or cancel.' },
      ],
      costMicros: 12000,
    });
    const result = await runTopicKbSweep({ pool, extractFn });
    expect(result.turnsScanned).toBe(15);
    expect(result.clustersExtracted).toBe(2);
    expect(result.insertedCount).toBe(2);
    expect(result.skippedDuplicateCount).toBe(0);
    expect(result.totalCostMicros).toBe(12000);
    expect(result.inserted).toHaveLength(2);
  });

  it('INSERT uses WHERE NOT EXISTS guard on question_normalized for idempotency', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(15) });
    queryFn.mockResolvedValueOnce({ rowCount: 1, rows: [] });
    const pool = makePool(queryFn);
    const extractFn = makeExtractFn({
      clusters: [{ question: 'How much does it cost?', answer: 'Sliding $19 to $39 a month.' }],
      costMicros: 5000,
    });
    await runTopicKbSweep({ pool, extractFn });
    const insertCall = queryFn.mock.calls.find((c) =>
      String(c[0]).includes('INSERT INTO support_kb')
    );
    expect(insertCall).toBeDefined();
    expect(insertCall![0]).toMatch(/WHERE NOT EXISTS/);
    expect(insertCall![0]).toMatch(/question_normalized = \$2/);
    expect(insertCall![1][0]).toBe('How much does it cost?');
    expect(insertCall![1][1]).toBe('how much does it cost'); // normalized
    expect(insertCall![1][2]).toBe('Sliding $19 to $39 a month.');
  });

  it('skippedDuplicateCount increments when WHERE NOT EXISTS matches existing row', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(15) });
    // INSERT returns rowCount=0 (the WHERE NOT EXISTS already had a match).
    queryFn.mockResolvedValueOnce({ rowCount: 0, rows: [] });
    const pool = makePool(queryFn);
    const extractFn = makeExtractFn({
      clusters: [{ question: 'How does pricing work?', answer: 'Sliding scale.' }],
      costMicros: 1000,
    });
    const result = await runTopicKbSweep({ pool, extractFn });
    expect(result.insertedCount).toBe(0);
    expect(result.skippedDuplicateCount).toBe(1);
  });

  it('skippedInvalidCount increments for clusters with em dashes', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(15) });
    const pool = makePool(queryFn);
    const extractFn = makeExtractFn({
      clusters: [
        { question: 'What is included?', answer: 'Hosting \u2014 plus revisions.' },
      ],
      costMicros: 1000,
    });
    const result = await runTopicKbSweep({ pool, extractFn });
    expect(result.insertedCount).toBe(0);
    expect(result.skippedInvalidCount).toBe(1);
  });

  it('caps inserts at maxClusters even if model returns more', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(20) });
    queryFn.mockResolvedValue({ rowCount: 1, rows: [] });
    const pool = makePool(queryFn);
    const extractFn = makeExtractFn({
      clusters: Array.from({ length: 8 }, (_, i) => ({
        question: `Question ${i}?`,
        answer: `Answer ${i}.`,
      })),
      costMicros: 8000,
    });
    const result = await runTopicKbSweep({ pool, extractFn, maxClusters: 3 });
    expect(result.insertedCount).toBe(3);
    expect(result.inserted).toHaveLength(3);
  });

  it('records totalCostMicros from extractFn output', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(15) });
    queryFn.mockResolvedValue({ rowCount: 1, rows: [] });
    const pool = makePool(queryFn);
    const extractFn = makeExtractFn({
      clusters: [{ question: 'A?', answer: 'B.' }],
      costMicros: 9999,
    });
    const result = await runTopicKbSweep({ pool, extractFn });
    expect(result.totalCostMicros).toBe(9999);
  });

  it('truncates the corpus passed to extractFn at corpusCharCap', async () => {
    const longText = 'A'.repeat(2000);
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({
      rows: Array.from({ length: 100 }, () => ({ keeper_text: longText })),
    });
    queryFn.mockResolvedValue({ rowCount: 1, rows: [] });
    const pool = makePool(queryFn);
    const extractFn = vi.fn().mockResolvedValue({ clusters: [], costMicros: 0 });
    await runTopicKbSweep({
      pool,
      corpusCharCap: 5_000,
      perTurnCharCap: 500,
      extractFn: extractFn as unknown as ExtractFn,
    });
    const corpus = (extractFn.mock.calls[0][0] as { corpus: string }).corpus;
    expect(corpus.length).toBeLessThanOrEqual(5_000);
  });
});

describe('buildCorpus', () => {
  it('truncates each turn at perTurnCap', () => {
    const out = buildCorpus(['A'.repeat(3000)], 100, 10_000);
    // "- " prefix + 100 chars
    expect(out.length).toBe(102);
  });

  it('stops appending when total exceeds corpusCap', () => {
    const out = buildCorpus(
      Array.from({ length: 100 }, () => 'X'.repeat(50)),
      100,
      300
    );
    expect(out.length).toBeLessThanOrEqual(300);
  });

  it('skips empty / whitespace-only turns', () => {
    const out = buildCorpus(['hello', '   ', '', 'world'], 100, 10_000);
    expect(out).toBe('- hello\n- world');
  });

  it('joins with newlines and "- " line prefix', () => {
    const out = buildCorpus(['one', 'two'], 100, 10_000);
    expect(out).toBe('- one\n- two');
  });
});

describe('parseClustersJson', () => {
  it('parses a valid JSON object with clusters array', () => {
    const raw = JSON.stringify({
      clusters: [{ question: 'Q', answer: 'A' }],
    });
    expect(parseClustersJson(raw)).toEqual([{ question: 'Q', answer: 'A' }]);
  });

  it('strips ```json fences', () => {
    const raw = '```json\n{"clusters": [{"question": "Q", "answer": "A"}]}\n```';
    expect(parseClustersJson(raw)).toEqual([{ question: 'Q', answer: 'A' }]);
  });

  it('returns [] on invalid JSON', () => {
    expect(parseClustersJson('not json')).toEqual([]);
  });

  it('returns [] when top-level is not an object', () => {
    expect(parseClustersJson('[]')).toEqual([]);
    expect(parseClustersJson('"hi"')).toEqual([]);
  });

  it('returns [] when clusters is missing or not an array', () => {
    expect(parseClustersJson('{}')).toEqual([]);
    expect(parseClustersJson('{"clusters": "nope"}')).toEqual([]);
  });

  it('drops malformed cluster entries', () => {
    const raw = JSON.stringify({
      clusters: [
        { question: 'Q1', answer: 'A1' },
        { question: '', answer: 'no question' },
        { question: 'no answer', answer: '' },
        'not an object',
        null,
        { question: 'Q2', answer: 'A2' },
      ],
    });
    expect(parseClustersJson(raw)).toEqual([
      { question: 'Q1', answer: 'A1' },
      { question: 'Q2', answer: 'A2' },
    ]);
  });

  it('trims question and answer whitespace', () => {
    const raw = JSON.stringify({
      clusters: [{ question: '  Q  ', answer: '\n A \n' }],
    });
    expect(parseClustersJson(raw)).toEqual([{ question: 'Q', answer: 'A' }]);
  });
});

describe('isValidCluster', () => {
  it('rejects clusters with em dashes in answer', () => {
    expect(
      isValidCluster({ question: 'Q', answer: 'A \u2014 B' } as TopicCluster)
    ).toBe(false);
  });

  it('rejects clusters with em dashes in question', () => {
    expect(
      isValidCluster({ question: 'Q \u2014 R', answer: 'A' } as TopicCluster)
    ).toBe(false);
  });

  it('rejects clusters with &mdash; HTML entities', () => {
    expect(
      isValidCluster({ question: 'Q', answer: 'A &mdash; B' } as TopicCluster)
    ).toBe(false);
  });

  it('rejects oversized question', () => {
    expect(
      isValidCluster({ question: 'X'.repeat(500), answer: 'A' } as TopicCluster)
    ).toBe(false);
  });

  it('rejects oversized answer', () => {
    expect(
      isValidCluster({ question: 'Q', answer: 'A'.repeat(2000) } as TopicCluster)
    ).toBe(false);
  });

  it('accepts a clean cluster', () => {
    expect(
      isValidCluster({
        question: 'How does it work?',
        answer: 'You start a conversation; we build the website.',
      } as TopicCluster)
    ).toBe(true);
  });
});

describe('normalizeQuestion', () => {
  it('lowercases and strips punctuation', () => {
    expect(normalizeQuestion('How DOES this work?!')).toBe('how does this work');
  });

  it('collapses whitespace', () => {
    expect(normalizeQuestion('  too   many   spaces  ')).toBe('too many spaces');
  });

  it('mirrors the support route normalizer (no em dashes -> empty token)', () => {
    expect(normalizeQuestion('A B C')).toBe('a b c');
  });
});

describe('buildExtractPrompt', () => {
  it('mentions the brand-voice rules and the no-em-dash rule', () => {
    const prompt = buildExtractPrompt({
      corpus: '- hi',
      maxClusters: 5,
      windowDays: 7,
    });
    expect(prompt).toMatch(/NO em dashes/);
    expect(prompt).toMatch(/website/);
    expect(prompt).toMatch(/last 7 days/);
    expect(prompt).toMatch(/clusters/);
    expect(prompt).toMatch(/JSON/);
  });

  it('does not itself contain em dashes (brand-voice for the prompt)', () => {
    const prompt = buildExtractPrompt({
      corpus: '- hi',
      maxClusters: 5,
      windowDays: 7,
    });
    expect(prompt.includes('\u2014')).toBe(false);
  });
});

describe('topic KB sweep input / output filtering (R1-W2 review)', () => {
  it('skips crisis-flagged turns and bot-classified trials and caps the row count', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: [] });
    await runTopicKbSweep({
      pool: makePool(queryFn),
      extractFn: makeExtractFn({ clusters: [], costMicros: 0 }),
    });
    const sql = queryFn.mock.calls[0][0] as string;
    expect(sql).toContain("NOT ('crisis' = ANY(ct.rail_flags))");
    expect(sql).toMatch(/COALESCE\(tk\.is_synthetic, FALSE\) = FALSE/);
    expect(sql).toMatch(/ORDER BY ct\.created_at DESC\s+LIMIT \$1/);
    expect(queryFn.mock.calls[0][1]).toEqual([5000]);
  });

  it('isValidCluster rejects pairs that carry a link, markup or an email address', () => {
    const bad = [
      'Visit https://example.test/offer for details',
      'See www.example.test for the offer',
      'Go to example.com/deal today',
      '<a href="x">click</a>',
      'Write to someone@example.test for a discount',
      'Use &lt;script&gt; here',
    ];
    for (const answer of bad) {
      expect(isValidCluster({ question: 'How does pricing work?', answer }), answer).toBe(false);
      expect(isValidCluster({ question: answer, answer: 'Fine.' }), answer).toBe(false);
    }
  });

  it('isValidCluster still accepts an ordinary support answer', () => {
    expect(
      isValidCluster({
        question: 'What is included in the subscription?',
        answer: 'Hosting, revisions, and a review of every build before it goes live.',
      })
    ).toBe(true);
  });

  it('isValidCluster rejects any dotted host name, an address and a phone number, not only a fixed list of TLDs', () => {
    const bad = [
      'You can pay at paypal.me/craftship before the build starts.',
      'Message the team on t.me/craftship or wa.me/15125550100.',
      'Visit example.shop for the latest pricing.',
      'Go to bit.ly/3abc for the form.',
      'The discount link is discord.gg/craft.',
      'Send the deposit through cash.app/$craftship.',
      'Pay at pay.example.co.uk today.',
      'Visit craftandship.io.example.test for the offer.',
      `Pay at paypal${String.fromCharCode(0xff0e)}me/craftship.`, // a full-width dot
      'The form is at 93.184.216.34/form.',
      'Call 512-555-0100 and give the code CRAFT50 for a refund.',
      'Text +1 (512) 555 0100 for a discount.',
    ];
    for (const answer of bad) {
      expect(isValidCluster({ question: 'How do I pay?', answer }), answer).toBe(false);
      expect(isValidCluster({ question: answer, answer: 'Fine.' }), answer).toBe(false);
    }
  });

  it('isValidCluster accepts abbreviations, prices and the product domain', () => {
    const fine = [
      'Pick any amount, e.g. $19 or $39, i.e. what fits your budget.',
      'Most sites are ready in 3.5 days; plans run $19.99 to $39 a month.',
      'Everything is explained at craftandship.io/pricing.',
      'Open 9 a.m. to 5 p.m., with up to 30 revisions in 2026.',
    ];
    for (const answer of fine) {
      expect(isValidCluster({ question: 'How does pricing work?', answer }), answer).toBe(true);
    }
  });

  it('containsLinkOrMarkup stays linear on long hostile text (isValidCluster caps it at 1,500 characters anyway)', () => {
    for (const unit of ['.', '.-', 'a.', 'a@', '1 (', 'word ']) {
      const text = unit.repeat(Math.ceil(200_000 / unit.length));
      const started = performance.now();
      containsLinkOrMarkup(text);
      expect(performance.now() - started, JSON.stringify(unit)).toBeLessThan(1000);
    }
  });

  it('stores what it inserts as pending, which the support chat does not serve', async () => {
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: makeTurnRows(15) });
    queryFn.mockResolvedValueOnce({ rowCount: 1, rows: [] });
    const extractFn = makeExtractFn({
      clusters: [{ question: 'How much does it cost?', answer: 'Sliding $19 to $39 a month.' }],
      costMicros: 5000,
    });
    await runTopicKbSweep({ pool: makePool(queryFn), extractFn });
    const insertSql = String(queryFn.mock.calls[1][0]);
    expect(insertSql).toMatch(/INSERT INTO support_kb \(question, question_normalized, answer, status\)\s+SELECT \$1, \$2, \$3, 'pending'/);
  });
});
