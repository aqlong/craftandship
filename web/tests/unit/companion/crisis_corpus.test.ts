import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { scanCrisis } from '../../../src/companion/crisis_detection';

/**
 * The crisis scan scored against phrasings that were NOT built from its
 * pattern list (crisis_corpus.fixture.json; its "about" field says how each
 * part was written).
 *
 * What the numbers mean
 * =====================
 * The 'dev', 'holdout' and 'holdout2' entries have all been used to adjust
 * the patterns, so the recall this file computes on them (in the high
 * nineties) is in-sample. It shows that the scan still does what it did; it
 * does not show how the scan does on text nobody has looked at.
 *
 * Two kinds of figure say something about unseen text.
 *
 * What the two independently written sets scored the FIRST time they were
 * run, before any pattern was adjusted to them:
 *
 *   holdout   54 of 68 crisis messages flagged (79%),  1 of 70 harmless ones
 *   holdout2  55 of 79 crisis messages flagged (70%),  4 of 80 harmless ones
 *
 * (holdout2 was written to be harder: indirect, hedged, or buried in a long
 * business message.) The scan this replaced flagged 15 of 68 and 19 of 79 of
 * the same messages, and 11 of 70 and 12 of 80 of the harmless ones. Those
 * first-run figures cannot be recomputed from this file; they are recorded
 * here so the in-sample figure is not mistaken for them.
 *
 * And the 'blind' split, which this file does recompute: forty plainly worded
 * disclosures and thirty harmless messages by another independent author,
 * written after the patterns were final and never used to change one. The
 * scan flags 39 of the 40 and none of the 30 (the scan it replaced: 20 and
 * 8). Plain wording is what that set measures; the first-run figures above
 * are the better guide for wording that is indirect, and a plan that is never
 * named is mostly missed.
 *
 * What the test asserts
 * =====================
 *  - Exactly which corpus entries the scan gets wrong (KNOWN_MISSES and
 *    KNOWN_FALSE_POSITIVES). A new miss fails the test; so does a fixed one,
 *    until it is taken off the list, so the lists cannot go stale.
 *  - Floors that hold even if those lists are edited: recall of at least 95%
 *    overall and 85% in every family, and at most 5% of harmless messages
 *    flagged (10% in any family). The floors sit a few entries below today's
 *    figures. Losing one normalization step or one family of patterns costs
 *    far more than that: the previous scan scores 30% and 22% on this corpus.
 */

interface CorpusRow {
  split: string;
  family: string;
  text: string;
}
interface Corpus {
  positives: CorpusRow[];
  negatives: CorpusRow[];
}

const everything = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'crisis_corpus.fixture.json'), 'utf-8')
) as Corpus;
const BLIND = 'blind';
/** The entries the patterns were adjusted to: the regression set. */
const corpus: Corpus = {
  positives: everything.positives.filter((row) => row.split !== BLIND),
  negatives: everything.negatives.filter((row) => row.split !== BLIND),
};
/** The entries no pattern was ever adjusted to. */
const blind: Corpus = {
  positives: everything.positives.filter((row) => row.split === BLIND),
  negatives: everything.negatives.filter((row) => row.split === BLIND),
};
/** The one blind disclosure the scan does not flag. Listed, not fixed. */
const BLIND_MISSES = ['I am planning to hurt a coworker badly.'];

/**
 * Crisis messages in the corpus that the scan does NOT flag. All but one are
 * plans spoken of without naming the plan, with a single cue (the scan needs
 * two; see PLANNING_CUES in crisis_detection.ts). The last is a threat in
 * words the scan treats as exasperation ("I could hurt him").
 */
const KNOWN_MISSES = [
  'i know how im going to do it',
  "i've been planning how im gonna do it and honestly the plan feels calm",
  'i decided on the date. its friday.',
  'I have a plan and honestly I feel calm about it, which is the scary part. Can we still finish the testimonials page tonight?',
  "I'm so angry I could actually hurt him. Not a figure of speech, I've been picturing it.",
];

/**
 * Harmless messages in the corpus that the scan DOES flag: figurative lines
 * that are word for word a disclosure, quoted speech, an emotional "hurt
 * me", and "I don't want to be here anymore" said about an office. Each
 * costs the sender one supportive message.
 */
const KNOWN_FALSE_POSITIVES = [
  "without your love I can't breathe, I can't sleep",
  "I'd rather be dead than live without your smile, goes the old ballad",
  'the character says "I want to die" in the opening scene',
  'my daughter hurt me when she said the logo looked dated',
  "I don't want to be here anymore, this office is too small, so I'm looking at a new space downtown",
];

const POSITIVE_FAMILIES = [
  'ideation-direct',
  'ideation-passive',
  'plan-means',
  'self-harm',
  'harm-others',
  'abuse-assault',
  'acute-medical',
  'third-party-risk',
];
const NEGATIVE_FAMILIES = ['idiom', 'figurative', 'third-person-news', 'negation', 'clinical', 'ordinary'];

const flagged = (row: CorpusRow): boolean => scanCrisis(row.text).flagged;
const share = (part: number, whole: number): number => (whole === 0 ? 0 : part / whole);

describe('crisis corpus: the fixture itself', () => {
  it('holds at least 150 crisis phrasings and 150 harmless ones written before the patterns were', () => {
    expect(corpus.positives.filter((row) => row.split === 'dev').length).toBeGreaterThanOrEqual(150);
    expect(corpus.negatives.filter((row) => row.split === 'dev').length).toBeGreaterThanOrEqual(150);
  });

  it('covers every family on both sides', () => {
    for (const family of POSITIVE_FAMILIES) {
      expect(corpus.positives.filter((row) => row.family === family).length, family).toBeGreaterThanOrEqual(20);
    }
    for (const family of NEGATIVE_FAMILIES) {
      expect(corpus.negatives.filter((row) => row.family === family).length, family).toBeGreaterThanOrEqual(40);
    }
    const known = new Set([...POSITIVE_FAMILIES, ...NEGATIVE_FAMILIES]);
    const unknown = [...corpus.positives, ...corpus.negatives].filter((row) => !known.has(row.family));
    expect(unknown).toEqual([]);
  });

  it('has no duplicate text and no text on both sides', () => {
    // Exact text: the same sentence in another case or spacing is a
    // deliberate variant.
    const texts = [...everything.positives, ...everything.negatives].map((row) => row.text);
    expect(new Set(texts).size).toBe(texts.length);
  });
});

describe('crisis corpus: the blind split (written after the patterns, never tuned to)', () => {
  it('holds forty disclosures and thirty harmless messages', () => {
    expect(blind.positives).toHaveLength(40);
    expect(blind.negatives).toHaveLength(30);
  });

  it('39 of the 40 disclosures are flagged, and the one that is not is the listed one', () => {
    const missed = blind.positives.filter((row) => !flagged(row)).map((row) => row.text);
    expect(missed).toEqual(BLIND_MISSES);
  });

  it('none of the 30 harmless messages is flagged', () => {
    expect(blind.negatives.filter((row) => flagged(row)).map((row) => row.text)).toEqual([]);
  });
});

describe('crisis corpus: what the scan catches and what it does not', () => {
  it('flags every crisis phrasing except the listed known misses', () => {
    const missed = corpus.positives.filter((row) => !flagged(row)).map((row) => row.text);
    expect(missed.sort()).toEqual([...KNOWN_MISSES].sort());
  });

  it('flags no harmless phrasing except the listed known false positives', () => {
    const wronglyFlagged = corpus.negatives.filter((row) => flagged(row)).map((row) => row.text);
    expect(wronglyFlagged.sort()).toEqual([...KNOWN_FALSE_POSITIVES].sort());
  });

  it('every listed miss and false positive is an entry of the corpus', () => {
    const positives = new Set(corpus.positives.map((row) => row.text));
    const negatives = new Set(corpus.negatives.map((row) => row.text));
    expect(KNOWN_MISSES.filter((text) => !positives.has(text))).toEqual([]);
    expect(KNOWN_FALSE_POSITIVES.filter((text) => !negatives.has(text))).toEqual([]);
  });
});

describe('crisis corpus: floors', () => {
  const RECALL_FLOOR = 0.95;
  const FAMILY_RECALL_FLOOR = 0.85;
  const FALSE_POSITIVE_CEILING = 0.05;
  const FAMILY_FALSE_POSITIVE_CEILING = 0.1;

  it(`recall is at least ${RECALL_FLOOR * 100}% overall and in each part of the corpus`, () => {
    expect(share(corpus.positives.filter(flagged).length, corpus.positives.length)).toBeGreaterThanOrEqual(RECALL_FLOOR);
    for (const split of ['dev', 'holdout', 'holdout2']) {
      const rows = corpus.positives.filter((row) => row.split === split);
      expect(rows.length, split).toBeGreaterThan(50);
      expect(share(rows.filter(flagged).length, rows.length), split).toBeGreaterThanOrEqual(RECALL_FLOOR);
    }
  });

  it(`recall is at least ${FAMILY_RECALL_FLOOR * 100}% in every family`, () => {
    for (const family of POSITIVE_FAMILIES) {
      const rows = corpus.positives.filter((row) => row.family === family);
      expect(share(rows.filter(flagged).length, rows.length), family).toBeGreaterThanOrEqual(FAMILY_RECALL_FLOOR);
    }
  });

  it(`at most ${FALSE_POSITIVE_CEILING * 100}% of harmless phrasings are flagged, and at most ${FAMILY_FALSE_POSITIVE_CEILING * 100}% in any family`, () => {
    expect(share(corpus.negatives.filter(flagged).length, corpus.negatives.length)).toBeLessThanOrEqual(FALSE_POSITIVE_CEILING);
    for (const family of NEGATIVE_FAMILIES) {
      const rows = corpus.negatives.filter((row) => row.family === family);
      expect(share(rows.filter(flagged).length, rows.length), family).toBeLessThanOrEqual(FAMILY_FALSE_POSITIVE_CEILING);
    }
  });
});
