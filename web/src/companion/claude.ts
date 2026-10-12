/**
 * Anthropic Claude SDK wrapper for the companion runtime.
 *
 * Centralizes:
 * - Sonnet model selection (default below; callers may pass `opts.model`)
 * - Prompt caching (ephemeral cache_control on the system prefix)
 * - Cost computation in micro-dollars (1 micro = $0.000001)
 * - Usage extraction (input, cached, output tokens)
 *
 * The SDK is already a project dependency (used by intake chat). We wrap
 * it here so the rest of companion/ does not depend on Anthropic SDK
 * shapes directly.
 */

import { AssistantNotConfiguredError } from '../utils/assistant';
import Anthropic from '@anthropic-ai/sdk';

// Model selection. Default to Sonnet 4.5 for daily companion conversation.
// Callers override per call via `opts.model` (e.g. opus for wedge moments).
export const DEFAULT_MODEL = 'claude-sonnet-4-5';

// Pricing (USD per million tokens), 2026 Anthropic published rates.
// Sonnet 4.5: $3 input, $15 output, cache write 1.25x, cache read 0.1x.
const PRICING_PER_MILLION_TOKENS: Record<
  string,
  { input: number; cacheWrite: number; cacheRead: number; output: number }
> = {
  'claude-sonnet-4-5': { input: 3, cacheWrite: 3.75, cacheRead: 0.3, output: 15 },
  'claude-opus-4-6': { input: 15, cacheWrite: 18.75, cacheRead: 1.5, output: 75 },
};

// Per-request limits. The SDK default is a 10 minute timeout with 2 retries,
// which would pin a request (and the keeper's spinner) for half an hour in the
// worst case. A companion reply is short; give up after a minute, retry once.
export const COMPANION_CALL_OPTIONS = { timeout: 60_000, maxRetries: 1 } as const;

let anthropicClient: Anthropic | null = null;

function getClient(): Anthropic {
  if (!anthropicClient) {
    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      throw new AssistantNotConfiguredError('claude: ANTHROPIC_API_KEY is not set');
    }
    anthropicClient = new Anthropic({ apiKey });
  }
  return anthropicClient;
}

/**
 * Reset the cached client. Test-only; do not call in production code.
 */
export function _resetClientForTests(): void {
  anthropicClient = null;
}

export interface CompanionMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface CallCompanionOpts {
  systemCacheable: string;     // persona + rails: cacheable prefix (~3-5K tokens)
  systemPerKeeper: string;     // memory snapshot: cacheable per-keeper (~1-3K tokens)
  recentTurns: CompanionMessage[]; // uncached tail
  keeperText: string;          // current keeper message
  model?: string;              // override DEFAULT_MODEL
  maxOutputTokens?: number;    // default 1024; companion replies are short
}

export interface CompanionCallResult {
  companionText: string;
  usage: {
    inputTokens: number;
    cachedInputTokens: number;
    cacheCreationTokens: number;
    outputTokens: number;
  };
  model: string;
  costMicros: number;
}

/**
 * Call Claude with a structured prompt for the companion runtime.
 *
 * The system prompt is split into two cache blocks: the universal
 * persona+rails prefix (cached across all keepers) and the per-keeper
 * memory snapshot (cached for that keeper until memory changes). Both
 * use ephemeral cache_control. The conversational tail (recent turns +
 * current keeper text) is uncached.
 */
export async function callCompanion(
  opts: CallCompanionOpts
): Promise<CompanionCallResult> {
  const model = opts.model ?? DEFAULT_MODEL;
  const maxTokens = opts.maxOutputTokens ?? 1024;

  const client = getClient();

  const systemBlocks = [
    { type: 'text' as const, text: opts.systemCacheable, cache_control: { type: 'ephemeral' as const } },
    { type: 'text' as const, text: opts.systemPerKeeper, cache_control: { type: 'ephemeral' as const } },
  ];

  const messages: CompanionMessage[] = [
    ...opts.recentTurns,
    { role: 'user', content: opts.keeperText },
  ];

  const response = await client.messages.create(
    {
      model,
      max_tokens: maxTokens,
      system: systemBlocks,
      messages,
    },
    COMPANION_CALL_OPTIONS
  );

  const textBlock = response.content.find((block) => block.type === 'text');
  if (!textBlock || textBlock.type !== 'text') {
    throw new Error('callCompanion: response had no text block');
  }

  const usage = {
    inputTokens: response.usage.input_tokens,
    cachedInputTokens: response.usage.cache_read_input_tokens ?? 0,
    cacheCreationTokens: response.usage.cache_creation_input_tokens ?? 0,
    outputTokens: response.usage.output_tokens,
  };

  return {
    companionText: textBlock.text,
    usage,
    model,
    costMicros: computeCostMicros(model, usage),
  };
}

/**
 * Compute total cost in micro-dollars for a single Claude call.
 * Returns 0 for unknown models (logs a warning) so cost logging never
 * crashes the turn.
 */
export function computeCostMicros(
  model: string,
  usage: {
    inputTokens: number;
    cachedInputTokens: number;
    cacheCreationTokens: number;
    outputTokens: number;
  }
): number {
  const pricing = PRICING_PER_MILLION_TOKENS[model];
  if (!pricing) {
    console.warn(`computeCostMicros: unknown model ${model}; cost will be 0`);
    return 0;
  }
  // Pricing is USD per 1M tokens. 1 USD = 1_000_000 micros.
  // So cost_micros = tokens * (price_per_million_usd) * (1_000_000 micros / 1 USD) / 1_000_000 tokens
  //                = tokens * price_per_million_usd
  const inputCost = usage.inputTokens * pricing.input;
  const cacheReadCost = usage.cachedInputTokens * pricing.cacheRead;
  const cacheWriteCost = usage.cacheCreationTokens * pricing.cacheWrite;
  const outputCost = usage.outputTokens * pricing.output;
  return Math.round(inputCost + cacheReadCost + cacheWriteCost + outputCost);
}
