/**
 * OpenAI text-embedding-3-small via REST.
 *
 * Pattern matches services/email.ts (Resend via fetch, no SDK). Avoids
 * adding the openai package as a dependency for a single endpoint.
 *
 * Pricing: $0.02 / 1M tokens (cheap; ~5K tokens per typical capture batch).
 * Returns 1536-dim float vectors plus the token count for cost logging.
 */

const OPENAI_EMBEDDINGS_URL = 'https://api.openai.com/v1/embeddings';
const OPENAI_EMBED_MODEL = 'text-embedding-3-small';
/** Embedding sits on the critical path of a turn; fail fast, callers degrade to recency-only. */
export const EMBED_TIMEOUT_MS = 10_000;
export const EMBEDDING_DIMENSIONS = 1536;

export interface EmbedResult {
  embeddings: number[][];
  totalTokens: number;
  model: string;
}

interface OpenAIEmbeddingResponse {
  data: Array<{ embedding: number[]; index: number }>;
  usage: { prompt_tokens: number; total_tokens: number };
  model: string;
}

/**
 * Embed one or more strings. Preserves input order in the returned
 * embeddings array. Throws on missing API key or non-2xx response.
 */
export async function embed(input: string | string[]): Promise<EmbedResult> {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    throw new Error('embed: OPENAI_API_KEY is not set');
  }

  const inputs = Array.isArray(input) ? input : [input];
  if (inputs.length === 0) {
    return { embeddings: [], totalTokens: 0, model: OPENAI_EMBED_MODEL };
  }

  const response = await fetch(OPENAI_EMBEDDINGS_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({ model: OPENAI_EMBED_MODEL, input: inputs }),
    signal: AbortSignal.timeout(EMBED_TIMEOUT_MS),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    throw new Error(
      `embed: OpenAI API ${response.status}: ${errorBody.slice(0, 500)}`
    );
  }

  const json = (await response.json()) as OpenAIEmbeddingResponse;
  // Sort by index to preserve input order; OpenAI usually returns in order
  // but the API contract allows any order.
  const sorted = [...json.data].sort((a, b) => a.index - b.index);
  return {
    embeddings: sorted.map((d) => d.embedding),
    totalTokens: json.usage.total_tokens,
    model: json.model,
  };
}

/**
 * Convenience for the common single-input case.
 */
export async function embedOne(text: string): Promise<{
  embedding: number[];
  totalTokens: number;
  model: string;
}> {
  const result = await embed(text);
  if (result.embeddings.length !== 1) {
    throw new Error(
      `embedOne: expected 1 embedding, got ${result.embeddings.length}`
    );
  }
  return {
    embedding: result.embeddings[0],
    totalTokens: result.totalTokens,
    model: result.model,
  };
}
