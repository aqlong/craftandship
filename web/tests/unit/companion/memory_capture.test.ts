import { describe, it, expect } from 'vitest';
import {
  parseClassifierJson,
  normalizeContent,
} from '../../../src/companion/memory_capture';

describe('parseClassifierJson', () => {
  it('parses a clean JSON array of items', () => {
    const raw = JSON.stringify([
      {
        category: 'audience',
        content: 'The keeper works with somatic practitioners.',
        keeper_phrase: 'I work with somatic folks',
        sacred_likelihood: 0.1,
      },
    ]);
    const items = parseClassifierJson(raw);
    expect(items).toHaveLength(1);
    expect(items[0].category).toBe('audience');
    expect(items[0].content).toMatch(/somatic/);
    expect(items[0].keeper_phrase).toBe('I work with somatic folks');
    expect(items[0].sacred_likelihood).toBe(0.1);
  });

  it('handles markdown code fences', () => {
    const raw = '```json\n[{"category":"fact","content":"The keeper teaches on Tuesday evenings."}]\n```';
    const items = parseClassifierJson(raw);
    expect(items).toHaveLength(1);
    expect(items[0].content).toMatch(/Tuesday/);
  });

  it('returns [] on invalid JSON', () => {
    expect(parseClassifierJson('not json at all')).toEqual([]);
    expect(parseClassifierJson('{not closed')).toEqual([]);
  });

  it('returns [] when output is not an array', () => {
    expect(parseClassifierJson('{"category":"fact","content":"x"}')).toEqual([]);
    expect(parseClassifierJson('"a string"')).toEqual([]);
  });

  it('drops items missing required fields', () => {
    const raw = JSON.stringify([
      { category: 'fact' }, // no content
      { content: 'x' }, // no category
      { category: 'fact', content: 'good' },
    ]);
    const items = parseClassifierJson(raw);
    expect(items).toHaveLength(1);
    expect(items[0].content).toBe('good');
  });

  it('drops items with invalid category', () => {
    const raw = JSON.stringify([
      { category: 'invalid-thing', content: 'x' },
      { category: 'fact', content: 'good' },
    ]);
    const items = parseClassifierJson(raw);
    expect(items).toHaveLength(1);
    expect(items[0].category).toBe('fact');
  });

  it('preserves sacred_likelihood for downstream partition decision', () => {
    const raw = JSON.stringify([
      {
        category: 'fear',
        content: 'The keeper is grieving a parent.',
        sacred_likelihood: 0.9,
      },
    ]);
    const items = parseClassifierJson(raw);
    expect(items[0].sacred_likelihood).toBe(0.9);
  });

  it('handles empty array (no captures)', () => {
    expect(parseClassifierJson('[]')).toEqual([]);
  });

  it('handles whitespace and trailing newlines', () => {
    const raw = '\n  [{"category":"fact","content":"ok"}]  \n\n';
    const items = parseClassifierJson(raw);
    expect(items).toHaveLength(1);
  });
});

describe('normalizeContent (lite-mode dedupe helper)', () => {
  it('lowercases', () => {
    expect(normalizeContent('Hello World')).toBe('hello world');
    expect(normalizeContent('SAMPLE KEEPER IS A TAROT READER')).toBe(
      'sample keeper is a tarot reader'
    );
  });

  it('trims leading and trailing whitespace', () => {
    expect(normalizeContent('   foo   ')).toBe('foo');
  });

  it('collapses internal whitespace runs to single spaces', () => {
    expect(normalizeContent('a  b   c')).toBe('a b c');
    expect(normalizeContent('a\n  b\tc')).toBe('a b c');
  });

  it('produces equal output for trivially-equivalent canonical statements', () => {
    const a =
      "The keeper's audience is somatic practitioners working with grief.";
    const b =
      "  THE KEEPER'S AUDIENCE IS  SOMATIC PRACTITIONERS WORKING WITH GRIEF.  ";
    expect(normalizeContent(a)).toBe(normalizeContent(b));
  });

  it('preserves real differences (does NOT collapse semantically distinct items)', () => {
    expect(normalizeContent('The keeper has 3 clients.')).not.toBe(
      normalizeContent('The keeper has 30 clients.')
    );
  });
});
