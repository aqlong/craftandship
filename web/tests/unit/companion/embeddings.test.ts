import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { embed, embedOne, EMBED_TIMEOUT_MS } from '../../../src/companion/embeddings';

describe('embeddings (OpenAI REST)', () => {
  const OLD_KEY = process.env.OPENAI_API_KEY;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    process.env.OPENAI_API_KEY = 'test-key-not-real';
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    if (OLD_KEY === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = OLD_KEY;
  });

  function okResponse(body: unknown): Response {
    return { ok: true, status: 200, json: async () => body, text: async () => '' } as unknown as Response;
  }

  it('sends an abort signal so a stalled provider cannot hang a turn', async () => {
    fetchMock.mockResolvedValueOnce(
      okResponse({
        data: [{ embedding: [1, 2, 3], index: 0 }],
        usage: { prompt_tokens: 2, total_tokens: 2 },
        model: 'text-embedding-3-small',
      })
    );
    await embed('hello');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(EMBED_TIMEOUT_MS).toBeLessThanOrEqual(10_000);
  });

  it('returns embeddings in input order even if the API reorders them', async () => {
    fetchMock.mockResolvedValueOnce(
      okResponse({
        data: [
          { embedding: [2], index: 1 },
          { embedding: [1], index: 0 },
        ],
        usage: { prompt_tokens: 4, total_tokens: 4 },
        model: 'm',
      })
    );
    const result = await embed(['a', 'b']);
    expect(result.embeddings).toEqual([[1], [2]]);
    expect(result.totalTokens).toBe(4);
  });

  it('does not call the network for an empty input list', async () => {
    const result = await embed([]);
    expect(result.embeddings).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('throws without a key, and never sends the key anywhere else', async () => {
    delete process.env.OPENAI_API_KEY;
    await expect(embed('x')).rejects.toThrow(/OPENAI_API_KEY is not set/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('truncates a non-2xx error body to 500 characters', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 429,
      text: async () => 'e'.repeat(2000),
    } as unknown as Response);
    await expect(embed('x')).rejects.toThrow(/OpenAI API 429: e{500}$/);
  });

  it('embedOne rejects when the provider returns a different number of vectors', async () => {
    fetchMock.mockResolvedValueOnce(
      okResponse({
        data: [
          { embedding: [1], index: 0 },
          { embedding: [2], index: 1 },
        ],
        usage: { prompt_tokens: 1, total_tokens: 1 },
        model: 'm',
      })
    );
    await expect(embedOne('x')).rejects.toThrow(/expected 1 embedding/);
  });
});
