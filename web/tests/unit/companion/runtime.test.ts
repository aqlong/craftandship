import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Pool } from 'pg';

// Mock the Claude SDK wrapper so no network calls fire.
vi.mock('../../../src/companion/claude', () => ({
  DEFAULT_MODEL: 'claude-sonnet-4-5',
  callCompanion: vi.fn(),
}));

// Mock memory capture: the runtime fires it as fire-and-forget. We don't
// want it pulling on the mocked pool with assertions we don't control.
vi.mock('../../../src/companion/memory_capture', () => ({
  captureMemory: vi.fn().mockResolvedValue(undefined),
}));

// Mock embeddings so memory retrieval's lite-mode path is exercised
// without a real OpenAI key. The runtime does not need this directly,
// but memory_retrieval.fetchRelevantMemory uses it.
vi.mock('../../../src/companion/embeddings', () => ({
  embedOne: vi.fn().mockResolvedValue({ embedding: [], totalTokens: 0 }),
}));

// Mock email service. The runtime crisis path now lazy-imports
// services/email and calls sendCrisisAlertEmail on detection. Mocking
// keeps the test deterministic (no Resend fetch, no isUnsubscribed
// pool.query call against the makePool() mock).
vi.mock('../../../src/services/email', () => ({
  sendCrisisAlertEmail: vi.fn().mockResolvedValue(undefined),
}));

import {
  turn,
  openingMessage,
  crisisReply,
  recordCrisisTurn,
  crisisSideEffects,
  createCrisisSideEffectBudget,
  CompanionDailyCostCapError,
  CRISIS_ALERT_COOLDOWN_MS,
  CRISIS_ALERT_MAX_PER_HOUR,
  CRISIS_BUDGET_MAX_KEYS,
  CRISIS_PERSIST_MAX_PER_MINUTE,
  CRISIS_PERSIST_MAX_PER_WINDOW,
  CRISIS_PERSIST_WINDOW_MS,
} from '../../../src/companion/runtime';
import { callCompanion } from '../../../src/companion/claude';

const mockCallCompanion = callCompanion as ReturnType<typeof vi.fn>;

/**
 * A mock pool with two programmable channels:
 *  - `spend` answers the daily-spend query that turn() runs for every
 *    model-backed turn (the per-identity and shared cost caps). It defaults
 *    to "nothing spent", which is what a test that is not about the caps
 *    needs; a test about the caps programs it.
 *  - `query` answers every other statement, in call order.
 * The runtime has no test-mode switch around the caps, so the fixture supplies
 * the spend figure instead.
 */
function makePool(): {
  pool: Pool;
  query: ReturnType<typeof vi.fn>;
  spend: ReturnType<typeof vi.fn>;
} {
  const query = vi.fn();
  const spend = vi.fn().mockResolvedValue({ rows: [{ total: '0', global_total: '0' }] });
  const routed = (sql: string, params?: unknown[]) =>
    /FROM companion_cost_log ccl/.test(String(sql)) ? spend(sql, params) : query(sql, params);
  return { pool: { query: routed } as unknown as Pool, query, spend };
}

const TRIAL_ID = '11111111-2222-3333-4444-555555555555';
const HAVEN_IDENTITY_ROW = {
  companion_name: 'Haven',
  started_at: new Date('2026-04-26T10:00:00Z'),
};

// "Not called" is asserted below; calls made by whichever test ran before must not count.
// The crisis side-effect budget is process-wide state, so each test starts with it empty.
beforeEach(() => {
  mockCallCompanion.mockReset();
  crisisSideEffects.reset();
});

describe('runtime.turn: opening message path', () => {
  it('returns the deterministic opening text without DB or Claude calls', async () => {
    const { pool, query } = makePool();
    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: '',
      isOpeningMessage: true,
    });
    expect(result.companionText).toBe(openingMessage());
    expect(result.turnId).toBeNull();
    expect(result.railFlags).toEqual([]);
    expect(result.costMicros).toBe(0);
    expect(query).not.toHaveBeenCalled();
    expect(mockCallCompanion).not.toHaveBeenCalled();
  });

  it('returned text mentions inviting any kind of opening', async () => {
    const { pool } = makePool();
    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: '',
      isOpeningMessage: true,
    });
    expect(result.companionText).toMatch(/what brings you here/i);
    expect(result.companionText).toMatch(/(no wrong|any way that feels true)/i);
  });
});

describe('runtime.turn: input validation', () => {
  it('throws when neither trialId nor keeperId is provided', async () => {
    const { pool } = makePool();
    await expect(
      turn({ pool, keeperText: 'hi' } as never)
    ).rejects.toThrow(/trialId or keeperId required/);
  });

  it('throws when both trialId and keeperId are provided', async () => {
    const { pool } = makePool();
    await expect(
      turn({ pool, trialId: 't', keeperId: 'k', keeperText: 'hi' })
    ).rejects.toThrow(/only one of/);
  });

  it('throws when keeperText is empty and isOpeningMessage is not set', async () => {
    const { pool } = makePool();
    await expect(
      turn({ pool, trialId: TRIAL_ID, keeperText: '' })
    ).rejects.toThrow(/keeperText is required/);
  });
});

describe('runtime.turn: crisis path', () => {
  beforeEach(() => {
    mockCallCompanion.mockReset();
  });

  it('short-circuits Claude when scanCrisis flags suicide; persists with rail_flags', async () => {
    const { pool, query } = makePool();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    // 1: identity SELECT, 2: persistTurn INSERT
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-1' }] });

    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: 'I want to kill myself',
    });

    expect(mockCallCompanion).not.toHaveBeenCalled();
    expect(result.railFlags).toContain('crisis');
    expect(result.railFlags).toContain('suicide');
    expect(result.turnId).toBe('turn-1');
    expect(result.costMicros).toBe(0);

    // persistTurn INSERT was the second call.
    const insertCall = query.mock.calls[1];
    const insertSql = insertCall[0] as string;
    const insertParams = insertCall[1] as unknown[];
    expect(insertSql).toMatch(/INSERT INTO companion_turns/);
    // Param order: keeper_id, trial_id, keeper_text, companion_text, rail_flags
    expect(insertParams[1]).toBe(TRIAL_ID);
    expect(insertParams[4]).toEqual(['crisis', 'suicide']);

    // notifyHarborMaster fires via console.warn (operational log path).
    expect(warnSpy).toHaveBeenCalledWith(
      '[harbor-master-notify] crisis flagged',
      expect.stringContaining('suicide')
    );
    warnSpy.mockRestore();
  });

  it('also fires sendCrisisAlertEmail (Phase 2 § 5.5: crisis email plumbing)', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-crisis-email' }] });

    const emailModule = await import('../../../src/services/email');
    const sendCrisisAlertSpy = emailModule.sendCrisisAlertEmail as ReturnType<typeof vi.fn>;
    sendCrisisAlertSpy.mockClear();

    await turn({ pool, trialId: TRIAL_ID, keeperText: 'I want to kill myself' });

    // Wait one microtask tick so the dynamic-import + await chain inside
    // notifyHarborMaster gets to flush. The runtime fires this without
    // awaiting at the turn-handler level; the test must let it land.
    await new Promise((resolve) => setImmediate(resolve));

    expect(sendCrisisAlertSpy).toHaveBeenCalledTimes(1);
    const arg = sendCrisisAlertSpy.mock.calls[0][0];
    expect(arg.categories).toContain('suicide');
    expect(arg.trialId).toBe(TRIAL_ID);
  });

  it('email-send failure does NOT cascade into a turn failure', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-resilient' }] });

    const emailModule = await import('../../../src/services/email');
    const sendCrisisAlertSpy = emailModule.sendCrisisAlertEmail as ReturnType<typeof vi.fn>;
    sendCrisisAlertSpy.mockClear();
    sendCrisisAlertSpy.mockRejectedValueOnce(new Error('Resend down'));
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    // Crisis turn should still resolve OK; the keeper still gets the
    // deterministic crisis script back even when email plumbing breaks.
    const result = await turn({ pool, trialId: TRIAL_ID, keeperText: 'I want to kill myself' });
    expect(result.railFlags).toContain('crisis');
    expect(result.turnId).toBe('turn-resilient');

    await new Promise((resolve) => setImmediate(resolve));
    // The error path should have logged.
    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('email send failed'),
      expect.any(String)
    );
    errSpy.mockRestore();
  });
});

describe('runtime.turn: input rail short-circuit', () => {
  beforeEach(() => {
    mockCallCompanion.mockReset();
  });

  it('short-circuits Claude when input rail flags an impersonation attempt', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-rail' }] });

    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: 'are you a real human?',
    });

    expect(mockCallCompanion).not.toHaveBeenCalled();
    expect(result.railFlags).toEqual(['ai-impersonation-attempt']);
    expect(result.companionText).toMatch(/I'm an AI/);
    expect(result.costMicros).toBe(0);
  });
});

describe('runtime.turn: full happy path persists cache_creation_tokens in cost log', () => {
  beforeEach(() => {
    mockCallCompanion.mockReset();
  });

  it('full pipeline: identity -> memory -> recent turns -> claude -> persist -> cost -> touch', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });    // identity
    query.mockResolvedValueOnce({                                    // memory SELECT
      rows: [
        {
          memory_id: 'm1',
          partition: 'working',
          category: 'audience',
          content: 'River produces dance.',
          keeper_phrase: null,
          embedding: null,
          updated_at: new Date(),
          created_at: new Date(),
          keeper_id: null,
          trial_id: TRIAL_ID,
          source_turn_id: null,
          last_retrieved_at: null,
          low_utility: false,
        },
      ],
    });
    query.mockResolvedValueOnce({ rows: [] });                       // stampLastRetrievedAt UPDATE
    query.mockResolvedValueOnce({                                    // recent turns
      rows: [
        { keeper_text: 'hi', companion_text: 'hello' },
        { keeper_text: 'how are you', companion_text: 'I am here' },
      ],
    });
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-x' }] });  // persistTurn
    query.mockResolvedValueOnce({ rows: [] });                       // logCost
    query.mockResolvedValueOnce({ rows: [] });                       // touchLastSeen

    mockCallCompanion.mockResolvedValueOnce({
      companionText: 'Tell me about your audience.',
      usage: { inputTokens: 50, cachedInputTokens: 0, cacheCreationTokens: 100, outputTokens: 20 },
      model: 'claude-sonnet-4-5',
      costMicros: 567,
    });

    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: 'I want a website.',
    });

    expect(result.companionText).toBe('Tell me about your audience.');
    expect(result.turnId).toBe('turn-x');
    expect(result.railFlags).toEqual([]);
    expect(result.costMicros).toBe(567);

    // Verify Claude was called with the prompt assembly:
    expect(mockCallCompanion).toHaveBeenCalledOnce();
    const claudeArgs = mockCallCompanion.mock.calls[0][0];
    expect(claudeArgs.systemCacheable).toMatch(/Haven/);
    expect(claudeArgs.systemPerKeeper).toMatch(/audience/); // memory snapshot in per-keeper block
    // Recent turns get expanded into 4 messages (2 turns × 2 sides) + the new keeper message.
    expect(claudeArgs.recentTurns).toHaveLength(4);
    expect(claudeArgs.keeperText).toBe('I want a website.');

    // Verify touchLastSeen UPDATE fired against trial_keepers.
    // Call sequence: identity, memory SELECT, stampLastRetrievedAt UPDATE,
    // recent turns, persistTurn INSERT, logCost, touchLastSeen.
    const touchSql = query.mock.calls[6][0] as string;
    expect(touchSql).toMatch(/UPDATE trial_keepers SET last_seen_at = NOW\(\)/);
  });

  it('output rail: dangerous flag -> entire response replaced with template', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [] }); // memory
    query.mockResolvedValueOnce({ rows: [] }); // recent turns
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-pers' }] }); // persistTurn
    query.mockResolvedValueOnce({ rows: [] }); // logCost
    query.mockResolvedValueOnce({ rows: [] }); // touchLastSeen

    mockCallCompanion.mockResolvedValueOnce({
      // Triggers output-claims-personhood pattern /\bi'?m a (real )?(human|person)\b/i.
      companionText: "I'm a real human and I really feel this. Tell me more.",
      usage: { inputTokens: 10, cachedInputTokens: 0, cacheCreationTokens: 0, outputTokens: 8 },
      model: 'claude-sonnet-4-5',
      costMicros: 100,
    });

    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: 'tell me about you',
    });

    expect(result.railFlags).toContain('output-claims-personhood');
    // Replacement template: "Let me try that again, honestly. I'm an AI..."
    expect(result.companionText).toMatch(/honestly/i);
    expect(result.companionText).not.toMatch(/I'm a real human/);
  });

  it("output rail: hygiene flag (em dash) -> response sanitized in place, not replaced", async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [] });
    query.mockResolvedValueOnce({ rows: [] });
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-hyg' }] });
    query.mockResolvedValueOnce({ rows: [] });
    query.mockResolvedValueOnce({ rows: [] });

    const original = 'A long valid reply with one em dash slip \u2014 like this \u2014 that should not nuke the whole reply.';
    mockCallCompanion.mockResolvedValueOnce({
      companionText: original,
      usage: { inputTokens: 10, cachedInputTokens: 0, cacheCreationTokens: 0, outputTokens: 30 },
      model: 'claude-sonnet-4-5',
      costMicros: 200,
    });

    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: 'hello',
    });
    expect(result.railFlags).toContain('output-uses-em-dash');
    // Response was sanitized + still long (close to original length, em dash replaced).
    expect(result.companionText).not.toContain('\u2014');
    expect(result.companionText.length).toBeGreaterThan(50);
    // The original semantic content is preserved.
    expect(result.companionText).toMatch(/A long valid reply/);
  });

  it('opens a NEW conversation: passes isFirstTurn=true to keeper context prompt', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [] }); // memory empty
    query.mockResolvedValueOnce({ rows: [] }); // recent turns empty -> isFirstTurn=true
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-first' }] });
    query.mockResolvedValueOnce({ rows: [] });
    query.mockResolvedValueOnce({ rows: [] });

    mockCallCompanion.mockResolvedValueOnce({
      companionText: 'OK.',
      usage: { inputTokens: 5, cachedInputTokens: 0, cacheCreationTokens: 0, outputTokens: 2 },
      model: 'claude-sonnet-4-5',
      costMicros: 50,
    });

    await turn({ pool, trialId: TRIAL_ID, keeperText: 'first message' });
    const claudeArgs = mockCallCompanion.mock.calls[0][0];
    expect(claudeArgs.recentTurns).toEqual([]);
    // isFirstTurn=true reaches the per-keeper prompt (persona.ts words it as "first message").
    expect(claudeArgs.systemPerKeeper).toMatch(/This is their first message/);
    expect(claudeArgs.systemPerKeeper).not.toMatch(/next message/);
    expect(claudeArgs.systemPerKeeper.length).toBeGreaterThan(0);
  });

  it('logCost INSERT includes cache_creation_tokens at column position 4', async () => {
    const { pool, query } = makePool();

    // Sequence of pool.query calls in the happy path:
    //   1. loadIdentity SELECT
    //   2. fetchRelevantMemory SELECT
    //   3. loadRecentTurns SELECT
    //   4. persistTurn INSERT (companion_turns)
    //   5. logCost INSERT (companion_cost_log)  <-- this is what we assert
    //   6. touchLastSeen UPDATE
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });    // identity
    query.mockResolvedValueOnce({ rows: [] });                       // memory
    query.mockResolvedValueOnce({ rows: [] });                       // recent turns
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-99' }] }); // persistTurn
    query.mockResolvedValueOnce({ rows: [] });                       // logCost
    query.mockResolvedValueOnce({ rows: [] });                       // touchLastSeen

    mockCallCompanion.mockResolvedValueOnce({
      companionText: 'Hi. Tell me about your work.',
      usage: {
        inputTokens: 100,
        cachedInputTokens: 50,
        cacheCreationTokens: 200,
        outputTokens: 30,
      },
      model: 'claude-sonnet-4-5',
      costMicros: 1234,
    });

    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: 'Tell me what you do here.',
    });

    expect(result.companionText).toBe('Hi. Tell me about your work.');
    expect(result.railFlags).toEqual([]);
    expect(result.costMicros).toBe(1234);

    // logCost call is the 5th SQL fired.
    const logCostCall = query.mock.calls[4];
    const logCostSql = logCostCall[0] as string;
    const logCostParams = logCostCall[1] as unknown[];

    expect(logCostSql).toMatch(/INSERT INTO companion_cost_log/);
    // Column order in the new INSERT: turn_id, input_tokens,
    // cached_input_tokens, cache_creation_tokens, output_tokens,
    // embedding_tokens, total_cost_micros, model.
    expect(logCostSql).toMatch(/cache_creation_tokens/);
    expect(logCostParams[0]).toBe('turn-99');
    expect(logCostParams[1]).toBe(100);  // inputTokens
    expect(logCostParams[2]).toBe(50);   // cachedInputTokens
    expect(logCostParams[3]).toBe(200);  // cacheCreationTokens (new column)
    expect(logCostParams[4]).toBe(30);   // outputTokens
    expect(logCostParams[5]).toBe(0);    // embeddingTokens (lite mode)
    expect(logCostParams[6]).toBe(1234); // totalCostMicros
    expect(logCostParams[7]).toBe('claude-sonnet-4-5');
  });
});

describe('runtime.turn: failure handling around the crisis rail (R1-W2 review)', () => {
  const CAP_ENV = 'CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS';
  let savedCap: string | undefined;

  beforeEach(() => {
    mockCallCompanion.mockReset();
    savedCap = process.env[CAP_ENV];
  });

  afterEach(() => {
    if (savedCap === undefined) delete process.env[CAP_ENV];
    else process.env[CAP_ENV] = savedCap;
    vi.restoreAllMocks();
  });

  it('crisis text is answered with the script even when the daily cost cap is spent', async () => {
    process.env[CAP_ENV] = '1000';
    const { pool, query, spend } = makePool();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    spend.mockResolvedValue({ rows: [{ total: '5000', global_total: '5000' }] }); // far over the cap
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] }); // identity
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-crisis-capped' }] }); // persist

    const result = await turn({ pool, trialId: TRIAL_ID, keeperText: 'I want to kill myself' });

    expect(result.railFlags).toContain('crisis');
    expect(result.companionText).toMatch(/988/);
    // The cost-cap SUM query must not have run for a crisis turn.
    expect(spend).not.toHaveBeenCalled();
    expect(mockCallCompanion).not.toHaveBeenCalled();
  });

  it('crisis script is still returned when persisting the turn fails (turnId null)', async () => {
    const { pool, query } = makePool();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockRejectedValueOnce(new Error('connection terminated'));

    const result = await turn({ pool, trialId: TRIAL_ID, keeperText: 'I want to die' });

    expect(result.railFlags).toEqual(['crisis', 'suicide']);
    expect(result.turnId).toBeNull();
    expect(result.companionText).toMatch(/988/);
    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('persisting the crisis turn failed'),
      'connection terminated'
    );
  });

  it('crisis script is still returned when the identity lookup fails (default name)', async () => {
    const { pool, query } = makePool();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    query.mockRejectedValueOnce(new Error('db down')); // identity
    query.mockRejectedValueOnce(new Error('db down')); // persist

    const result = await turn({ pool, trialId: TRIAL_ID, keeperText: 'I want to kill myself' });

    expect(result.railFlags).toContain('crisis');
    expect(result.companionText).toContain('Haven');
    expect(result.turnId).toBeNull();
  });

  it('crisis text with a typographic apostrophe is still caught', async () => {
    const { pool, query } = makePool();
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-curly' }] });
    const curly = String.fromCharCode(0x2019);

    const result = await turn({
      pool,
      trialId: TRIAL_ID,
      keeperText: `I don${curly}t want to live anymore`,
    });

    expect(result.railFlags).toContain('crisis');
    expect(mockCallCompanion).not.toHaveBeenCalled();
  });

  it('throws CompanionDailyCostCapError (and never calls Claude) when a normal turn is over cap', async () => {
    process.env[CAP_ENV] = '1000';
    const { pool, query, spend } = makePool();
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
    spend.mockResolvedValueOnce({ rows: [{ total: '5000' }] }); // SUM over the cap

    await expect(
      turn({ pool, trialId: TRIAL_ID, keeperText: 'Tell me about my audience.' })
    ).rejects.toBeInstanceOf(CompanionDailyCostCapError);
    expect(mockCallCompanion).not.toHaveBeenCalled();
    // Nothing was persisted: identity + SUM only.
    expect(query).toHaveBeenCalledTimes(1);
    expect(spend).toHaveBeenCalledTimes(1);
  });

  it('throws the SHARED cap error when everyone together is over the global ceiling, even if this identity is not', async () => {
    const GLOBAL_ENV = 'CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS';
    const savedGlobal = process.env[GLOBAL_ENV];
    process.env[CAP_ENV] = '1000000';
    process.env[GLOBAL_ENV] = '2000';
    try {
      const { pool, query, spend } = makePool();
      query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
      // This trial has spent almost nothing; all identities together are over.
      spend.mockResolvedValueOnce({ rows: [{ total: '10', global_total: '2500' }] });

      const failure = await turn({ pool, trialId: TRIAL_ID, keeperText: 'Tell me about my audience.' }).catch(
        (err: unknown) => err
      );
      expect(failure).toBeInstanceOf(CompanionDailyCostCapError);
      expect((failure as CompanionDailyCostCapError).scope).toBe('shared');
      expect((failure as CompanionDailyCostCapError).capMicros).toBe(2000);
      expect(mockCallCompanion).not.toHaveBeenCalled();
      expect(query).toHaveBeenCalledTimes(1); // identity
      expect(spend).toHaveBeenCalledTimes(1); // one SUM query for both caps
    } finally {
      if (savedGlobal === undefined) delete process.env[GLOBAL_ENV];
      else process.env[GLOBAL_ENV] = savedGlobal;
    }
  });

  it('the crisis script is answered even when the shared ceiling is spent', async () => {
    const GLOBAL_ENV = 'CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS';
    const savedGlobal = process.env[GLOBAL_ENV];
    process.env[CAP_ENV] = '1000000';
    process.env[GLOBAL_ENV] = '1';
    try {
      const { pool, query, spend } = makePool();
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
      query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-crisis' }] });
      const result = await turn({ pool, trialId: TRIAL_ID, keeperText: 'I want to kill myself' });
      expect(result.companionText).toMatch(/988/);
      // Answered without consulting the spend (the cap would have refused) and without a model call.
      expect(spend).not.toHaveBeenCalled();
      expect(mockCallCompanion).not.toHaveBeenCalled();
    } finally {
      if (savedGlobal === undefined) delete process.env[GLOBAL_ENV];
      else process.env[GLOBAL_ENV] = savedGlobal;
    }
  });

  it('a cost-log insert failure after the reply is persisted does not fail the turn', async () => {
    const { pool, query } = makePool();
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] }); // identity
    query.mockResolvedValueOnce({ rows: [] }); // memory
    query.mockResolvedValueOnce({ rows: [] }); // recent turns
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-persisted' }] }); // persist
    query.mockRejectedValueOnce(new Error('cost insert failed')); // logCost
    query.mockRejectedValueOnce(new Error('touch failed')); // touchLastSeen
    mockCallCompanion.mockResolvedValueOnce({
      companionText: 'Tell me more.',
      usage: { inputTokens: 5, cachedInputTokens: 0, cacheCreationTokens: 0, outputTokens: 2 },
      model: 'claude-sonnet-4-5',
      costMicros: 321,
    });

    const result = await turn({ pool, trialId: TRIAL_ID, keeperText: 'Hello there' });

    expect(result.turnId).toBe('turn-persisted');
    expect(result.companionText).toBe('Tell me more.');
    // Claude was called exactly once and the turn row was written exactly once.
    expect(mockCallCompanion).toHaveBeenCalledTimes(1);
    const turnInserts = query.mock.calls.filter((c) =>
      String(c[0]).includes('INSERT INTO companion_turns')
    );
    expect(turnInserts).toHaveLength(1);
    // The lost figures are written to the log so the cost can be rebuilt.
    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('[companion-cost-log]'),
      expect.stringContaining('"totalCostMicros":321'),
      'cost insert failed'
    );
  });
});


describe('runtime.turn: the daily caps apply in every environment', () => {
  const CAP_ENV = 'CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS';
  const GLOBAL_ENV = 'CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS';
  const saved = { cap: process.env[CAP_ENV], global: process.env[GLOBAL_ENV], nodeEnv: process.env.NODE_ENV };

  afterEach(() => {
    for (const [name, value] of [[CAP_ENV, saved.cap], [GLOBAL_ENV, saved.global], ['NODE_ENV', saved.nodeEnv]] as const) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });

  it.each(['test', 'development', 'production'])(
    'NODE_ENV=%s with no override: the spend query runs and the $5 default refuses an identity that has spent it',
    async (nodeEnv) => {
      process.env.NODE_ENV = nodeEnv;
      delete process.env[CAP_ENV];
      delete process.env[GLOBAL_ENV];
      const { pool, query, spend } = makePool();
      query.mockResolvedValueOnce({ rows: [HAVEN_IDENTITY_ROW] });
      spend.mockResolvedValueOnce({ rows: [{ total: '5000000', global_total: '5000000' }] });

      const failure = await turn({ pool, trialId: TRIAL_ID, keeperText: 'Tell me about my audience.' }).catch(
        (err: unknown) => err
      );

      expect(failure).toBeInstanceOf(CompanionDailyCostCapError);
      expect((failure as CompanionDailyCostCapError).capMicros).toBe(5_000_000);
      expect(spend).toHaveBeenCalledTimes(1);
      expect(mockCallCompanion).not.toHaveBeenCalled();
    }
  );
});

describe('crisisReply: the fixed script, with nothing to wait for', () => {
  it('is a pure function of the categories and uses the default companion name', () => {
    const reply = crisisReply(['suicide']);
    expect(reply.companionText).toMatch(/988/);
    expect(reply.companionText).toContain('Haven');
    expect(reply.railFlags).toEqual(['crisis', 'suicide']);
    expect(crisisReply(['suicide'])).toEqual(reply);
  });

  it('adds the emergency-number line for abuse', () => {
    expect(crisisReply(['abuse']).companionText).toMatch(/911/);
  });
});

describe('recordCrisisTurn: what a crisis reply leaves behind is bounded', () => {
  // The alert is fired without being awaited (a lazy import, then the send),
  // so wait for the expected number to land, then a little longer to catch
  // any that should not have been sent.
  async function settled(send: ReturnType<typeof vi.fn>, expected: number): Promise<void> {
    await vi.waitFor(() => expect(send.mock.calls.length).toBeGreaterThanOrEqual(expected));
    await new Promise((resolve) => setTimeout(resolve, 30));
  }

  let warnSpy: ReturnType<typeof vi.spyOn>;
  /** What each crisis log line said about its alert email, in order. */
  function alertDecisions(): string[] {
    return warnSpy.mock.calls
      .filter((call) => call[0] === '[harbor-master-notify] crisis flagged')
      .map((call) => (JSON.parse(String(call[1])) as { alertEmail: string }).alertEmail);
  }

  async function emailSpy(): Promise<ReturnType<typeof vi.fn>> {
    const emailModule = await import('../../../src/services/email');
    const spy = emailModule.sendCrisisAlertEmail as ReturnType<typeof vi.fn>;
    spy.mockReset().mockResolvedValue(undefined);
    return spy;
  }

  beforeEach(() => {
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('stores the turn flagged, with the message cut to 4000 characters, and alerts once', async () => {
    const { pool, query } = makePool();
    const send = await emailSpy();
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-stored' }] });
    const reply = crisisReply(['suicide']);

    const turnId = await recordCrisisTurn({
      pool,
      trialId: TRIAL_ID,
      keeperText: 'I want to kill myself ' + 'x'.repeat(6000),
      reply,
    });
    await settled(send, 1);

    expect(turnId).toBe('turn-stored');
    const [sql, params] = query.mock.calls[0] as [string, unknown[]];
    expect(sql).toMatch(/INSERT INTO companion_turns/);
    expect(params[0]).toBeNull(); // keeper_id
    expect(params[1]).toBe(TRIAL_ID); // trial_id
    expect(String(params[2])).toHaveLength(4000);
    expect(params[3]).toBe(reply.companionText);
    expect(params[4]).toEqual(['crisis', 'suicide']);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][0]).toMatchObject({ trialId: TRIAL_ID, categories: ['suicide'] });
  });

  it('files a keeper turn under the keeper id', async () => {
    const { pool, query } = makePool();
    await emailSpy();
    query.mockResolvedValueOnce({ rows: [{ turn_id: 'turn-keeper' }] });
    await recordCrisisTurn({ pool, keeperId: 'keeper-1', keeperText: 'I want to die', reply: crisisReply(['suicide']) });
    const params = query.mock.calls[0][1] as unknown[];
    expect(params[0]).toBe('keeper-1');
    expect(params[1]).toBeNull();
  });

  it('with no identity: nothing is stored (there is no conversation to store it in), the alert still goes', async () => {
    const { pool, query } = makePool();
    const send = await emailSpy();
    const turnId = await recordCrisisTurn({ pool, keeperText: 'I want to die', reply: crisisReply(['suicide']) });
    await settled(send, 1);
    expect(turnId).toBeNull();
    expect(query).not.toHaveBeenCalled();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('a flood from one identity: one alert email, and stored turns stop at the per-window budget', async () => {
    const { pool, query } = makePool();
    const send = await emailSpy();
    query.mockResolvedValue({ rows: [{ turn_id: 'turn-n' }] });
    const reply = crisisReply(['suicide']);

    const results: Array<string | null> = [];
    for (let i = 0; i < CRISIS_PERSIST_MAX_PER_WINDOW + 15; i += 1) {
      results.push(await recordCrisisTurn({ pool, trialId: TRIAL_ID, keeperText: 'I want to die', reply }));
    }
    await settled(send, 1);

    expect(results.filter((id) => id !== null)).toHaveLength(CRISIS_PERSIST_MAX_PER_WINDOW);
    expect(query).toHaveBeenCalledTimes(CRISIS_PERSIST_MAX_PER_WINDOW);
    expect(send).toHaveBeenCalledTimes(1);
    // The crisis itself is logged every time, email or not.
    const decisions = alertDecisions();
    expect(decisions).toHaveLength(CRISIS_PERSIST_MAX_PER_WINDOW + 15);
    expect(decisions.filter((d) => d === 'sending')).toHaveLength(1);
    expect(decisions.filter((d) => d === 'suppressed (cooldown)')).toHaveLength(CRISIS_PERSIST_MAX_PER_WINDOW + 14);
  });

  it('a flood across many identities: alert emails stop at the hourly cap for the whole process', async () => {
    const { pool, query } = makePool();
    const send = await emailSpy();
    query.mockResolvedValue({ rows: [{ turn_id: 'turn-n' }] });
    const reply = crisisReply(['suicide']);

    for (let i = 0; i < CRISIS_ALERT_MAX_PER_HOUR * 3; i += 1) {
      await recordCrisisTurn({ pool, trialId: `trial-${i}`, keeperText: 'I want to die', reply });
    }
    await settled(send, CRISIS_ALERT_MAX_PER_HOUR);

    expect(send).toHaveBeenCalledTimes(CRISIS_ALERT_MAX_PER_HOUR);
    // Every one of them was still stored and logged.
    expect(query).toHaveBeenCalledTimes(CRISIS_ALERT_MAX_PER_HOUR * 3);
    const decisions = alertDecisions();
    expect(decisions).toHaveLength(CRISIS_ALERT_MAX_PER_HOUR * 3);
    expect(decisions.filter((d) => d === 'suppressed (hourly_cap)')).toHaveLength(CRISIS_ALERT_MAX_PER_HOUR * 2);
  });

  it('never throws: an insert failure is logged and reported as null', async () => {
    const { pool, query } = makePool();
    await emailSpy();
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    query.mockRejectedValueOnce(new Error('db down'));
    await expect(
      recordCrisisTurn({ pool, trialId: TRIAL_ID, keeperText: 'I want to die', reply: crisisReply(['suicide']) })
    ).resolves.toBeNull();
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining('persisting the crisis turn failed'), 'db down');
  });
});

describe('createCrisisSideEffectBudget', () => {
  it('lets an identity alert again after the cooldown, and the hourly cap frees up as alerts age out', () => {
    let nowMs = 1_000_000;
    const budget = createCrisisSideEffectBudget({ now: () => nowMs });

    expect(budget.allowAlert('trial:a')).toBe('send');
    expect(budget.allowAlert('trial:a')).toBe('cooldown');
    nowMs += CRISIS_ALERT_COOLDOWN_MS;
    expect(budget.allowAlert('trial:a')).toBe('send');

    for (let i = 0; i < CRISIS_ALERT_MAX_PER_HOUR - 2; i += 1) {
      expect(budget.allowAlert(`trial:other-${i}`)).toBe('send');
    }
    expect(budget.allowAlert('trial:one-too-many')).toBe('hourly_cap');
    // A refusal by the hourly cap does not start that identity's cooldown.
    nowMs += 60 * 60 * 1000;
    expect(budget.allowAlert('trial:one-too-many')).toBe('send');
  });

  it('stored-turn budget is per identity and renews with the window', () => {
    let nowMs = 5_000_000;
    const budget = createCrisisSideEffectBudget({ now: () => nowMs });
    for (let i = 0; i < CRISIS_PERSIST_MAX_PER_WINDOW; i += 1) {
      expect(budget.allowPersist('trial:a')).toBe(true);
    }
    expect(budget.allowPersist('trial:a')).toBe(false);
    expect(budget.allowPersist('trial:b')).toBe(true);
    nowMs += CRISIS_PERSIST_WINDOW_MS;
    expect(budget.allowPersist('trial:a')).toBe(true);
  });

  it('reset() forgets everything', () => {
    const budget = createCrisisSideEffectBudget();
    expect(budget.allowAlert('k')).toBe('send');
    budget.reset();
    expect(budget.allowAlert('k')).toBe('send');
  });
});

// The crisis path is answered ahead of every limiter, so what it stores has to
// stay bounded however many identities send crisis messages.
describe('crisis stored-turn budget: bounded map, process-wide cap, the reply always sent', () => {
  // Enough for every identity below to be allowed, so only the map is under test.
  const UNCAPPED = Number.MAX_SAFE_INTEGER;

  it('keeps the map at its limit by evicting the oldest entries, not by clearing it', () => {
    let nowMs = 1_000_000;
    const budget = createCrisisSideEffectBudget({ now: () => nowMs, maxPersistPerMinute: UNCAPPED });
    for (let i = 0; i <= CRISIS_BUDGET_MAX_KEYS; i += 1) {
      expect(budget.allowPersist(`trial:flood-${i}`)).toBe(true);
    }
    expect(budget.trackedIdentities()).toBe(CRISIS_BUDGET_MAX_KEYS);
    // The newest identity is still tracked; the oldest was the one dropped.
    expect(budget.allowPersist(`trial:flood-${CRISIS_BUDGET_MAX_KEYS}`)).toBe(true);
    expect(budget.trackedIdentities()).toBe(CRISIS_BUDGET_MAX_KEYS);
  });

  it('an identity that used its quota is still limited after other identities are evicted', () => {
    let nowMs = 2_000_000;
    const budget = createCrisisSideEffectBudget({ now: () => nowMs, maxPersistPerMinute: UNCAPPED });
    for (let i = 0; i < CRISIS_PERSIST_MAX_PER_WINDOW; i += 1) {
      expect(budget.allowPersist('trial:quota-used')).toBe(true);
    }
    expect(budget.allowPersist('trial:quota-used')).toBe(false);
    for (let i = 0; i <= CRISIS_BUDGET_MAX_KEYS; i += 1) {
      budget.allowPersist(`trial:flood-${i}`);
    }
    expect(budget.allowPersist('trial:quota-used')).toBe(false);
    expect(budget.trackedIdentities()).toBe(CRISIS_BUDGET_MAX_KEYS);
    // Within the same window: the quota does not renew because the map filled up.
    nowMs += CRISIS_PERSIST_WINDOW_MS - 1;
    expect(budget.allowPersist('trial:quota-used')).toBe(false);
  });

  it('stores at most CRISIS_PERSIST_MAX_PER_MINUTE turns per minute across all identities, and the minute renews', () => {
    let nowMs = 9_000_000;
    const budget = createCrisisSideEffectBudget({ now: () => nowMs });
    for (let i = 0; i < CRISIS_PERSIST_MAX_PER_MINUTE; i += 1) {
      expect(budget.allowPersist(`trial:minute-${i}`)).toBe(true);
    }
    expect(budget.allowPersist('trial:one-more')).toBe(false);
    // An identity already holding a row is refused too: the cap is process-wide.
    expect(budget.allowPersist('trial:minute-0')).toBe(false);
    nowMs += 60 * 1000;
    expect(budget.allowPersist('trial:one-more')).toBe(true);
  });

  it('a turn over the cap still gets the fixed crisis reply, with no row stored and a log line', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-12T12:00:00Z'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const emailModule = await import('../../../src/services/email');
    (emailModule.sendCrisisAlertEmail as ReturnType<typeof vi.fn>).mockReset().mockResolvedValue(undefined);
    try {
      const { pool, query } = makePool();
      query.mockResolvedValue({ rows: [{ ...HAVEN_IDENTITY_ROW, turn_id: 'turn-stored' }] });
      for (let i = 0; i < CRISIS_PERSIST_MAX_PER_MINUTE; i += 1) {
        expect(await recordCrisisTurn({ pool, trialId: `trial-cap-${i}`, keeperText: 'I want to die', reply: crisisReply(['suicide']) })).toBe('turn-stored');
      }
      const inserts = () => query.mock.calls.filter((c) => /INSERT INTO companion_turns/.test(String(c[0]))).length;
      expect(inserts()).toBe(CRISIS_PERSIST_MAX_PER_MINUTE);

      const result = await turn({ pool, trialId: 'trial-over-cap', keeperText: 'I want to die' });
      expect(result.companionText).toBe(crisisReply(['suicide'], 'Haven').companionText);
      expect(result.railFlags).toEqual(['crisis', 'suicide']);
      expect(result.turnId).toBeNull();
      expect(inserts()).toBe(CRISIS_PERSIST_MAX_PER_MINUTE);
      expect(warn.mock.calls.some((c) => String(c[0]).includes('crisis turn not stored'))).toBe(true);

      expect(await recordCrisisTurn({ pool, trialId: 'trial-over-cap-2', keeperText: 'I want to die', reply: crisisReply(['suicide']) })).toBeNull();
      expect(inserts()).toBe(CRISIS_PERSIST_MAX_PER_MINUTE);
    } finally {
      vi.useRealTimers();
      warn.mockRestore();
    }
  });
});
