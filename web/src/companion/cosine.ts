/**
 * Cosine similarity for two equal-length number vectors.
 *
 * Used by memory_retrieval.ts to rank candidate keeper_memory rows
 * against a query embedding, JS-side. At MVP scale (max ~3K rows per
 * keeper) a full table scan plus this function is comfortably fast.
 *
 * Returns a value in [-1, 1] where 1 means identical direction.
 * Returns 0 if either vector has zero magnitude (avoids NaN).
 */
export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length !== b.length) {
    throw new Error(
      `cosineSimilarity: vector length mismatch (${a.length} vs ${b.length})`
    );
  }
  let dot = 0;
  let magA = 0;
  let magB = 0;
  for (let i = 0; i < a.length; i++) {
    const av = a[i];
    const bv = b[i];
    dot += av * bv;
    magA += av * av;
    magB += bv * bv;
  }
  if (magA === 0 || magB === 0) {
    return 0;
  }
  return dot / (Math.sqrt(magA) * Math.sqrt(magB));
}
