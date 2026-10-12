import { describe, it, expect } from 'vitest';
import { cosineSimilarity } from '../../../src/companion/cosine';

describe('cosineSimilarity', () => {
  it('returns 1 for identical vectors', () => {
    expect(cosineSimilarity([1, 0, 0], [1, 0, 0])).toBeCloseTo(1, 10);
    expect(cosineSimilarity([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])).toBeCloseTo(1, 10);
  });

  it('returns 0 for orthogonal vectors', () => {
    expect(cosineSimilarity([1, 0, 0], [0, 1, 0])).toBeCloseTo(0, 10);
  });

  it('returns -1 for antiparallel vectors', () => {
    expect(cosineSimilarity([1, 1, 0], [-1, -1, 0])).toBeCloseTo(-1, 10);
  });

  it('is invariant to magnitude (scale)', () => {
    const small = cosineSimilarity([1, 2, 3], [4, 5, 6]);
    const big = cosineSimilarity([1, 2, 3], [40, 50, 60]);
    expect(big).toBeCloseTo(small, 10);
  });

  it('returns 0 when either vector is zero-magnitude (no NaN)', () => {
    expect(cosineSimilarity([0, 0, 0], [1, 2, 3])).toBe(0);
    expect(cosineSimilarity([1, 2, 3], [0, 0, 0])).toBe(0);
    expect(cosineSimilarity([0, 0, 0], [0, 0, 0])).toBe(0);
  });

  it('throws on mismatched vector lengths', () => {
    expect(() => cosineSimilarity([1, 2], [1, 2, 3])).toThrow(/length mismatch/);
  });

  it('handles 1536-dim vectors (text-embedding-3-small size)', () => {
    const a = new Array(1536).fill(0).map((_, i) => Math.sin(i));
    const b = new Array(1536).fill(0).map((_, i) => Math.sin(i + 0.001));
    const sim = cosineSimilarity(a, b);
    expect(sim).toBeGreaterThan(0.99);
    expect(sim).toBeLessThanOrEqual(1);
  });
});
