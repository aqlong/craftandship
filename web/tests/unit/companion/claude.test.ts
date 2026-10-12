import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const createMock = vi.fn();

vi.mock('@anthropic-ai/sdk', () => {
  class FakeAnthropic {
    messages = { create: createMock };
  }
  return { default: FakeAnthropic };
});

import {
  callCompanion,
  computeCostMicros,
  COMPANION_CALL_OPTIONS,
  _resetClientForTests,
} from '../../../src/companion/claude';

describe('computeCostMicros', () => {
  it('prices Sonnet tokens in whole micro-dollars (1 micro = $0.000001)', () => {
    // 1M input tokens at $3/M = $3 = 3,000,000 micros
    expect(
      computeCostMicros('claude-sonnet-4-5', {
        inputTokens: 1_000_000,
        cachedInputTokens: 0,
        cacheCreationTokens: 0,
        outputTokens: 0,
      })
    ).toBe(3_000_000);
    // 100 input, 50 cache read, 200 cache write, 30 output
    // = 100*3 + 50*0.3 + 200*3.75 + 30*15 = 300 + 15 + 750 + 450 = 1515
    expect(
      computeCostMicros('claude-sonnet-4-5', {
        inputTokens: 100,
        cachedInputTokens: 50,
        cacheCreationTokens: 200,
        outputTokens: 30,
      })
    ).toBe(1515);
  });

  it('always returns an integer even when the fractional cache-read price leaves a remainder', () => {
    const micros = computeCostMicros('claude-sonnet-4-5', {
      inputTokens: 1,
      cachedInputTokens: 3, // 3 * 0.3 = 0.9
      cacheCreationTokens: 1, // 3.75
      outputTokens: 1,
    });
    expect(Number.isInteger(micros)).toBe(true);
    expect(micros).toBe(Math.round(3 + 0.9 + 3.75 + 15));
  });

  it('returns 0 and warns for an unknown model instead of throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(
      computeCostMicros('made-up-model', {
        inputTokens: 10,
        cachedInputTokens: 0,
        cacheCreationTokens: 0,
        outputTokens: 10,
      })
    ).toBe(0);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('unknown model'));
    warn.mockRestore();
  });
});

describe('callCompanion', () => {
  const OLD_KEY = process.env.ANTHROPIC_API_KEY;

  beforeEach(() => {
    createMock.mockReset();
    _resetClientForTests();
    process.env.ANTHROPIC_API_KEY = 'test-key-not-real';
  });

  afterEach(() => {
    if (OLD_KEY === undefined) delete process.env.ANTHROPIC_API_KEY;
    else process.env.ANTHROPIC_API_KEY = OLD_KEY;
  });

  const baseOpts = {
    systemCacheable: 'persona',
    systemPerKeeper: 'keeper context',
    recentTurns: [{ role: 'user' as const, content: 'earlier' }],
    keeperText: 'now',
  };

  it('bounds the call with a timeout and a single retry', async () => {
    createMock.mockResolvedValueOnce({
      content: [{ type: 'text', text: 'hello' }],
      usage: { input_tokens: 10, output_tokens: 5 },
    });
    await callCompanion(baseOpts);
    expect(createMock).toHaveBeenCalledTimes(1);
    const [body, options] = createMock.mock.calls[0];
    expect(options).toEqual(COMPANION_CALL_OPTIONS);
    expect(options.timeout).toBeLessThanOrEqual(60_000);
    expect(options.maxRetries).toBe(1);
    expect(body.max_tokens).toBe(1024);
  });

  it('marks both system blocks cacheable and appends the keeper text last', async () => {
    createMock.mockResolvedValueOnce({
      content: [{ type: 'text', text: 'hello' }],
      usage: { input_tokens: 10, output_tokens: 5 },
    });
    await callCompanion(baseOpts);
    const [body] = createMock.mock.calls[0];
    expect(body.system).toHaveLength(2);
    for (const block of body.system) {
      expect(block.cache_control).toEqual({ type: 'ephemeral' });
    }
    expect(body.messages[body.messages.length - 1]).toEqual({ role: 'user', content: 'now' });
  });

  it('maps usage including cache tokens and prices the call', async () => {
    createMock.mockResolvedValueOnce({
      content: [{ type: 'text', text: 'hello' }],
      usage: {
        input_tokens: 100,
        cache_read_input_tokens: 50,
        cache_creation_input_tokens: 200,
        output_tokens: 30,
      },
    });
    const result = await callCompanion(baseOpts);
    expect(result.usage).toEqual({
      inputTokens: 100,
      cachedInputTokens: 50,
      cacheCreationTokens: 200,
      outputTokens: 30,
    });
    expect(result.costMicros).toBe(1515);
    expect(result.companionText).toBe('hello');
  });

  it('throws when the response has no text block (for example a refusal)', async () => {
    createMock.mockResolvedValueOnce({
      content: [],
      usage: { input_tokens: 1, output_tokens: 0 },
    });
    await expect(callCompanion(baseOpts)).rejects.toThrow(/no text block/);
  });

  it('throws a clear error when ANTHROPIC_API_KEY is not set', async () => {
    delete process.env.ANTHROPIC_API_KEY;
    _resetClientForTests();
    await expect(callCompanion(baseOpts)).rejects.toThrow(/ANTHROPIC_API_KEY is not set/);
  });
});
