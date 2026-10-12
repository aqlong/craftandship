// Anonymous traffic cannot spend the whole daily model-call budget.
//
// The budget (services/llm-budget.ts) is one pool. This file runs the three
// pieces that meet there, each for real, around a model client that answers
// from memory:
//
//   routes/anon-intake.ts    asks for an anonymous admission, then
//   services/claude.ts       takes the calls out of the pool, and
//   services/spec_extraction.ts  takes one call for a customer's extraction.
//
// Mocked: the database pool, the draft store behind /try, and the Anthropic
// SDK's client (so services/claude.ts itself, with its breaker and its budget
// gate, is the real module). The budget is set small so a test can spend it.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import supertest from 'supertest';
import type { Request } from 'express';
import type { Pool } from 'pg';

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: vi.fn(), on: vi.fn() },
}));

// One model client for every module that builds one. It answers a chat turn
// with a sentence; the spec-preview call gets the same sentence, which is not
// JSON, and services/claude.ts treats that as "no preview" (still one call).
const modelCalls = vi.hoisted(() => ({ count: 0 }));
vi.mock('@anthropic-ai/sdk', () => {
  class FakeAnthropic {
    messages = {
      create: async () => {
        modelCalls.count += 1;
        return {
          content: [{ type: 'text', text: 'Tell me a little more about your studio.' }],
          usage: { input_tokens: 1, output_tokens: 1 },
          stop_reason: 'end_turn',
        };
      },
    };
  }
  return { default: FakeAnthropic };
});

vi.mock('../../src/services/anon-intake', () => {
  let seq = 0;
  return {
    generateAnonSessionId: vi.fn(() => 'a'.repeat(64)),
    createAnonDraft: vi.fn(async () => {
      seq += 1;
      return { buildId: `draft-${seq}` };
    }),
    getAnonDraftByBuildId: vi.fn(),
    claimAnonDraft: vi.fn(),
    claimAnonDraftBySessionId: vi.fn(),
  };
});
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));
vi.mock('../../src/services/stripe', () => ({}));

const ENV_KEYS = ['ANTHROPIC_API_KEY', 'CAS_LLM_DAILY_CALL_CAP', 'CAS_HARBOR_ENABLED'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of ENV_KEYS) savedEnv[key] = process.env[key];
// Assembled at runtime: present so the routes do not answer "not configured",
// never sent anywhere (the client is the double above).
process.env.ANTHROPIC_API_KEY = ['not', 'a', 'real', 'key'].join('-');
delete process.env.CAS_HARBOR_ENABLED;

import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import { extractSpecFromConversation } from '../../src/services/spec_extraction';
import {
  ANONYMOUS_CALL_SHARE,
  LlmDailyBudgetError,
  anonymousCallBudget,
  anonymousCallsAdmitted,
  dailyCallBudget,
  dailyCallsUsed,
  releaseAnonymousCalls,
  resetDailyCallBudget,
  tryAdmitAnonymousCalls,
  tryConsumeDailyCall,
} from '../../src/services/llm-budget';

const csrf = (): string => generateCsrfToken({} as Request);
const DAY_MS = 24 * 60 * 60 * 1000;

/** One anonymous first message: a draft, the reply and the spec preview (two model calls). */
async function anonymousFirstMessage(): Promise<supertest.Response> {
  return supertest.agent(app).post('/try/start').set('x-csrf-token', csrf()).send({ message: 'A site for my yoga studio' });
}

/** A paying keeper's extraction, on a pool that holds a conversation for them. */
async function keeperExtraction(): Promise<unknown> {
  const query = vi.fn();
  query.mockResolvedValueOnce({ rows: [{ email: 'keeper@example.com' }] });
  query.mockResolvedValueOnce({ rows: [{ category: 'practice', content: 'reiki', keeper_phrase: null }] });
  query.mockResolvedValueOnce({ rows: [{ keeper_text: 'hi', companion_text: 'tell me more' }] });
  query.mockResolvedValue({ rows: [] });
  const client = {
    messages: {
      create: vi.fn().mockResolvedValue({
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              spec_version: '1.0',
              app_name: 'keeper-site',
              description: 'A site',
              tier: 'build',
              pages: [{ name: 'Home', slug: '/', sections: [] }],
            }),
          },
        ],
        usage: { input_tokens: 1, output_tokens: 1 },
      }),
    },
  };
  return extractSpecFromConversation({
    pool: { query } as unknown as Pool,
    keeperCustomerId: 'c0ffee00-0000-4000-8000-000000000002',
    client: client as never,
  });
}

beforeEach(() => {
  mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
  modelCalls.count = 0;
  resetDailyCallBudget();
});

afterEach(() => {
  resetDailyCallBudget();
});

describe('the anonymous share (services/llm-budget.ts)', () => {
  afterEach(() => {
    if (savedEnv.CAS_LLM_DAILY_CALL_CAP === undefined) delete process.env.CAS_LLM_DAILY_CALL_CAP;
    else process.env.CAS_LLM_DAILY_CALL_CAP = savedEnv.CAS_LLM_DAILY_CALL_CAP;
  });

  it('is a fixed part of whatever the budget is, rounded down, and zero when the budget is zero', () => {
    expect(ANONYMOUS_CALL_SHARE).toBe(0.8);
    delete process.env.CAS_LLM_DAILY_CALL_CAP;
    expect(anonymousCallBudget()).toBe(Math.floor(dailyCallBudget() * 0.8));
    process.env.CAS_LLM_DAILY_CALL_CAP = '10';
    expect(anonymousCallBudget()).toBe(8);
    process.env.CAS_LLM_DAILY_CALL_CAP = '3';
    expect(anonymousCallBudget()).toBe(2);
    process.env.CAS_LLM_DAILY_CALL_CAP = '0';
    expect(anonymousCallBudget()).toBe(0);
    expect(tryAdmitAnonymousCalls(1)).toBe(false);
  });

  it('admits whole requests only: a request that would cross the line is refused and counts nothing', () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '10';
    expect(tryAdmitAnonymousCalls(2)).toBe(true);
    expect(tryAdmitAnonymousCalls(2)).toBe(true);
    expect(tryAdmitAnonymousCalls(2)).toBe(true);
    expect(anonymousCallsAdmitted()).toBe(6);
    expect(tryAdmitAnonymousCalls(3)).toBe(false); // 6 + 3 > 8
    expect(anonymousCallsAdmitted()).toBe(6);
    expect(tryAdmitAnonymousCalls(2)).toBe(true); // 8, the line itself
    expect(tryAdmitAnonymousCalls(1)).toBe(false);
  });

  it('admissions are not calls: the pool is only drawn on when a call is made', () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '10';
    expect(tryAdmitAnonymousCalls(8)).toBe(true);
    expect(dailyCallsUsed()).toBe(0);
  });

  it('rolls over with the same 24-hour window as the pool', () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '10';
    // The window is measured from the reset, to the millisecond, so the clock
    // is held still while the window is opened: otherwise `start` could fall
    // a millisecond after it and "one millisecond before the end" would
    // already be past it.
    vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-03-01T12:00:00Z') });
    try {
      resetDailyCallBudget();
      const start = Date.now();
      expect(tryAdmitAnonymousCalls(8, start)).toBe(true);
      expect(tryAdmitAnonymousCalls(1, start + DAY_MS - 1)).toBe(false);
      expect(tryAdmitAnonymousCalls(8, start + DAY_MS)).toBe(true);
      // The pool's own counter was reset by the same roll.
      expect(dailyCallsUsed()).toBe(0);
      // And a roll caused by the pool resets the admissions.
      expect(tryConsumeDailyCall(start + 2 * DAY_MS)).toBe(true);
      expect(anonymousCallsAdmitted()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('an admission for a call that was never made can be given back, and then admits someone else', () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '10';
    const now = Date.now();
    expect(tryAdmitAnonymousCalls(8, now)).toBe(true);
    expect(tryAdmitAnonymousCalls(1, now)).toBe(false);
    releaseAnonymousCalls(1, now, now);
    expect(anonymousCallsAdmitted()).toBe(7);
    expect(tryAdmitAnonymousCalls(1, now)).toBe(true);
    // The pool is untouched by either: admissions are not calls.
    expect(dailyCallsUsed()).toBe(0);
  });

  it('giving back never goes below zero, and an admission from a window that has since rolled over gives nothing back', () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '10';
    vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-03-01T12:00:00Z') });
    try {
      resetDailyCallBudget();
      const start = Date.now();
      releaseAnonymousCalls(3, start, start);
      expect(anonymousCallsAdmitted()).toBe(0);

      // Admitted late in one window; the request ends in the next, after
      // other visitors were admitted there. Their admissions are not its to
      // give back.
      expect(tryAdmitAnonymousCalls(1, start + DAY_MS - 5)).toBe(true);
      expect(tryAdmitAnonymousCalls(4, start + DAY_MS + 5)).toBe(true);
      releaseAnonymousCalls(1, start + DAY_MS - 5, start + DAY_MS + 10);
      expect(anonymousCallsAdmitted()).toBe(4);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('a flood of anonymous turns leaves a paying customer\'s extraction its calls', () => {
  beforeEach(() => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '10'; // anonymous share: 8 calls, four first messages
  });
  afterEach(() => {
    if (savedEnv.CAS_LLM_DAILY_CALL_CAP === undefined) delete process.env.CAS_LLM_DAILY_CALL_CAP;
    else process.env.CAS_LLM_DAILY_CALL_CAP = savedEnv.CAS_LLM_DAILY_CALL_CAP;
  });

  it('anonymous first messages are answered until their share is spent, then refused; the extraction still runs', async () => {
    const statuses: number[] = [];
    for (let i = 0; i < 12; i += 1) {
      statuses.push((await anonymousFirstMessage()).status);
    }
    // Four are served (two model calls each); every later one is refused
    // before a draft is created or the model is asked.
    expect(statuses.slice(0, 4)).toEqual([200, 200, 200, 200]);
    expect(statuses.slice(4).every((status) => status === 503)).toBe(true);
    expect(modelCalls.count).toBe(8);
    expect(dailyCallsUsed()).toBe(8);

    // Two calls of the ten are left, and they are not anonymous traffic's to take.
    await expect(keeperExtraction()).resolves.toMatchObject({ spec: { app_name: 'keeper-site' } });
    expect(dailyCallsUsed()).toBe(9);
  });

  it('the pool itself still ends: once every call is spent, the extraction is refused like anything else', async () => {
    for (let i = 0; i < 4; i += 1) await anonymousFirstMessage();
    expect(tryConsumeDailyCall()).toBe(true);
    expect(tryConsumeDailyCall()).toBe(true);
    await expect(keeperExtraction()).rejects.toBeInstanceOf(LlmDailyBudgetError);
  });
});
