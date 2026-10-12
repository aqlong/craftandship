// Tests for the Anthropic Admin API cost fetcher.
// Critical regression guard: the API's `amount` field is a decimal string in
// the currency's lowest unit (cents for USD), so it is divided by 100 to get
// dollars. An earlier edit removed the /100 and made the cost dashboard show
// 100x the real spend.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  getAnthropicCosts,
  getAnthropicCostsAndStatus,
  clearCache,
} from '../../src/services/anthropic-costs';

// Synthetic fixture, built at runtime so no secret-shaped literal appears in source.
const FAKE_ADMIN_KEY = 'sk-' + 'ant-admin01-' + 'fake-key-for-tests';

const ORIGINAL_KEY = process.env.ANTHROPIC_ADMIN_KEY;

describe('getAnthropicCosts', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    process.env.ANTHROPIC_ADMIN_KEY = FAKE_ADMIN_KEY;
    clearCache();
    fetchSpy = vi.spyOn(global, 'fetch') as unknown as ReturnType<typeof vi.spyOn>;
    // Freeze ONLY Date at this file's design date. The mocked API buckets are
    // hardcoded to 2026-04-2x; with the real clock, those dates age out of the
    // trailing 90-day request window and every bucket silently filters to
    // empty (first observed 2026-07-21, ~90 days after the design date).
    // toFake:['Date'] leaves setTimeout real so retry/backoff tests still run.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-04-27T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
    fetchSpy.mockRestore();
    if (ORIGINAL_KEY === undefined) {
      delete process.env.ANTHROPIC_ADMIN_KEY;
    } else {
      process.env.ANTHROPIC_ADMIN_KEY = ORIGINAL_KEY;
    }
    clearCache();
  });

  it('returns null immediately during failure cooldown without calling API', async () => {
    vi.useFakeTimers();
    // First call: API returns 403 (hard failure, no partial data)
    fetchSpy.mockResolvedValueOnce(new Response('Forbidden', { status: 403 }));
    const r1 = await getAnthropicCosts(7);
    expect(r1).toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // Second call within cooldown: should return null WITHOUT hitting the API
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [], has_more: false, next_page: null,
    }), { status: 200 }));
    const r2 = await getAnthropicCosts(7);
    expect(r2).toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(1); // still 1; no second fetch

    // Advance past the 60s cooldown
    vi.advanceTimersByTime(61000);

    // Third call after cooldown: should try the API again
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [], has_more: false, next_page: null,
    }), { status: 200 }));
    const r3 = await getAnthropicCosts(7);
    expect(r3).not.toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(2); // now 2; cooldown expired

    vi.useRealTimers();
  });

  it('returns null when ANTHROPIC_ADMIN_KEY is unset', async () => {
    delete process.env.ANTHROPIC_ADMIN_KEY;
    const result = await getAnthropicCosts(7);
    expect(result).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('divides amount by 100 to convert API value to actual USD', async () => {
    // The API's `amount` field is reported in some scaled unit (despite
    // `currency: USD`). Empirically: a single-day API sum that totals to
    // ~2910 raw maps to ~$29 of actual spend. Cross-checked against an
    // actual Anthropic invoice: the API sum divided by 100 matches the bill.
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [{
        starting_at: '2026-04-20T00:00:00Z',
        ending_at: '2026-04-21T00:00:00Z',
        results: [
          { currency: 'USD', amount: '1.7508', workspace_id: null, description: 'Haiku Input', cost_type: 'tokens', model: 'claude-haiku-4-5-20251001', token_type: 'uncached_input_tokens' },
          { currency: 'USD', amount: '5.9185', workspace_id: null, description: 'Haiku Output', cost_type: 'tokens', model: 'claude-haiku-4-5-20251001', token_type: 'output_tokens' },
          { currency: 'USD', amount: '2903.2535', workspace_id: null, description: 'Opus Input', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'uncached_input_tokens' },
        ],
      }],
      has_more: false,
      next_page: null,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));

    const result = await getAnthropicCosts(90);
    expect(result).not.toBeNull();
    expect(result).toHaveLength(1);
    const day = result![0];
    expect(day.date).toBe('2026-04-20');
    // Raw sum: 1.7508 + 5.9185 + 2903.2535 = 2910.9228; divide by 100.
    expect(day.total_usd).toBeCloseTo(29.109228, 4);
    // Per-model breakdown also divided by 100
    expect(day.by_model['claude-haiku-4-5']).toBeCloseTo((1.7508 + 5.9185) / 100, 4);
    expect(day.by_model['claude-opus-4-6']).toBeCloseTo(2903.2535 / 100, 4);
  });

  it('aggregates multiple buckets across days', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [
        {
          starting_at: '2026-04-20T00:00:00Z',
          ending_at: '2026-04-21T00:00:00Z',
          results: [
            { currency: 'USD', amount: '10.50', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' },
          ],
        },
        {
          starting_at: '2026-04-21T00:00:00Z',
          ending_at: '2026-04-22T00:00:00Z',
          results: [
            { currency: 'USD', amount: '20.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' },
            { currency: 'USD', amount: '5.25', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-sonnet-4-6', token_type: 'output_tokens' },
          ],
        },
      ],
      has_more: false,
      next_page: null,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));

    const result = await getAnthropicCosts(90);
    expect(result).toHaveLength(2);
    const sorted = result!.slice().sort((a, b) => a.date.localeCompare(b.date));
    expect(sorted[0].date).toBe('2026-04-20');
    // Raw 10.50 / 100 = 0.105
    expect(sorted[0].total_usd).toBeCloseTo(0.105, 4);
    expect(sorted[1].date).toBe('2026-04-21');
    // Raw 20.00 + 5.25 = 25.25, / 100 = 0.2525
    expect(sorted[1].total_usd).toBeCloseTo(0.2525, 4);
  });

  it('returns null when API returns non-OK status', async () => {
    fetchSpy.mockResolvedValueOnce(new Response('Forbidden', { status: 403 }));
    const result = await getAnthropicCosts(7);
    expect(result).toBeNull();
  });

  it('returns null when fetch throws', async () => {
    fetchSpy.mockRejectedValueOnce(new Error('network'));
    const result = await getAnthropicCosts(7);
    expect(result).toBeNull();
  });

  it('caches the 90d fetch and serves any subrange without a second API call', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [{
        starting_at: '2026-04-20T00:00:00Z',
        ending_at: '2026-04-21T00:00:00Z',
        results: [{ currency: 'USD', amount: '7.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
      }],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    const r90 = await getAnthropicCosts(90);
    const r30 = await getAnthropicCosts(30); // should NOT trigger a second fetch
    const r7 = await getAnthropicCosts(7);   // same
    expect(r90![0].total_usd).toBeCloseTo(0.07, 4); // 7.00 / 100
    // All three reuse the same single fetch
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    // r30 and r7 are subsets: the fixture date 2026-04-20 is outside a 7/30d
    // window relative to today so both will be empty arrays (not null)
    expect(r30).not.toBeNull();
    expect(r7).not.toBeNull();
  });

  it('filters cached data to requested range without a second fetch', async () => {
    // Two dates: yesterday (within 7d) and 8 days ago (outside 7d, within 90d)
    const yesterday = new Date();
    yesterday.setUTCDate(yesterday.getUTCDate() - 1);
    const yesterdayStr = yesterday.toISOString().slice(0, 10);

    const eightDaysAgo = new Date();
    eightDaysAgo.setUTCDate(eightDaysAgo.getUTCDate() - 8);
    const eightDaysAgoStr = eightDaysAgo.toISOString().slice(0, 10);

    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [
        {
          starting_at: yesterdayStr + 'T00:00:00Z',
          ending_at: yesterdayStr + 'T00:00:00Z',
          results: [{ currency: 'USD', amount: '100.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
        },
        {
          starting_at: eightDaysAgoStr + 'T00:00:00Z',
          ending_at: eightDaysAgoStr + 'T00:00:00Z',
          results: [{ currency: 'USD', amount: '200.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
        },
      ],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    const r90 = await getAnthropicCosts(90);
    expect(r90).toHaveLength(2); // both dates within 90d

    const r7 = await getAnthropicCosts(7);
    expect(r7).toHaveLength(1);           // only yesterday is within 7d
    expect(r7![0].date).toBe(yesterdayStr);
    expect(r7![0].total_usd).toBeCloseTo(1.00, 4); // 100.00 / 100

    expect(fetchSpy).toHaveBeenCalledTimes(1); // single fetch served both ranges
  });

  it('returns partial data when mid-pagination fetch fails', async () => {
    // Page 1 succeeds with one bucket; page 2 returns 500.
    const page1 = new Response(JSON.stringify({
      data: [{
        starting_at: '2026-04-20T00:00:00Z',
        ending_at: '2026-04-21T00:00:00Z',
        results: [{ currency: 'USD', amount: '500.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
      }],
      has_more: true,
      next_page: 'cursor-abc',
    }), { status: 200 });
    const page2 = new Response('Internal Server Error', { status: 500 });

    fetchSpy.mockResolvedValueOnce(page1).mockResolvedValueOnce(page2);

    const result = await getAnthropicCosts(90);
    // Must return partial data (page 1), not null.
    expect(result).not.toBeNull();
    expect(result).toHaveLength(1);
    expect(result![0].date).toBe('2026-04-20');
    expect(result![0].total_usd).toBeCloseTo(5.0, 4); // 500.00 / 100
  });

  it('returns null when first page fails (no partial data to return)', async () => {
    fetchSpy.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));
    const result = await getAnthropicCosts(7);
    expect(result).toBeNull();
  });

  it('URL-encodes the pagination cursor in the next request', async () => {
    const page1 = new Response(JSON.stringify({
      data: [{
        starting_at: '2026-04-20T00:00:00Z',
        ending_at: '2026-04-21T00:00:00Z',
        results: [{ currency: 'USD', amount: '100.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
      }],
      has_more: true,
      next_page: 'cur+sor/with=chars&x',
    }), { status: 200 });
    const page2 = new Response(JSON.stringify({ data: [], has_more: false, next_page: null }), { status: 200 });
    fetchSpy.mockResolvedValueOnce(page1).mockResolvedValueOnce(page2);

    await getAnthropicCosts(90);

    const secondUrl = fetchSpy.mock.calls[1][0] as string;
    expect(secondUrl).toContain('&page=cur%2Bsor%2Fwith%3Dchars%26x');
    expect(secondUrl).not.toContain('&page=cur+sor');
  });

  it('caps a huge Retry-After and does not sleep after the final 429', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-27T12:00:00Z'));
    const setTimeoutSpy = vi.spyOn(global, 'setTimeout');
    fetchSpy.mockImplementation(async () =>
      new Response('slow down', { status: 429, headers: { 'retry-after': '3600' } })
    );

    const promise = getAnthropicCosts(7);
    await vi.runAllTimersAsync();
    const result = await promise;
    vi.useRealTimers();

    expect(result).toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(4);
    // 4 attempts means 3 sleeps (never after the last one), each capped at 30s
    // even though the server asked for an hour.
    const sleeps = setTimeoutSpy.mock.calls
      .map((c) => c[1] as number)
      .filter((ms) => typeof ms === 'number' && ms >= 1000 && ms !== 10000);
    expect(sleeps).toEqual([30000, 30000, 30000]);
    setTimeoutSpy.mockRestore();
  });

  it('retries on 429 then succeeds on the next attempt', async () => {
    vi.useFakeTimers();
    const rateLimited = new Response(JSON.stringify({ type: 'error', error: { type: 'rate_limit_error' } }), { status: 429 });
    const success = new Response(JSON.stringify({
      data: [{
        starting_at: '2026-04-20T00:00:00Z',
        ending_at: '2026-04-21T00:00:00Z',
        results: [{ currency: 'USD', amount: '300.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
      }],
      has_more: false,
      next_page: null,
    }), { status: 200 });

    fetchSpy.mockResolvedValueOnce(rateLimited).mockResolvedValueOnce(success);

    const resultPromise = getAnthropicCosts(90);
    // Advance past the 2000ms initial retry delay
    await vi.runAllTimersAsync();
    const result = await resultPromise;
    vi.useRealTimers();

    expect(result).not.toBeNull();
    expect(result![0].total_usd).toBeCloseTo(3.0, 4); // 300.00 / 100
    expect(fetchSpy).toHaveBeenCalledTimes(2); // 1 rate-limited + 1 success
  });

  it('does not include group_by[] in the request URL', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    await getAnthropicCosts(7);
    const url = (fetchSpy.mock.calls[0][0] as string);
    expect(url).not.toContain('group_by');
    expect(url).toContain('bucket_width=1d');
  });

  it('pins limit=31 on the request URL to match Admin API max page size', async () => {
    // Bug 2026-05-18: without an explicit limit, the Admin API returned a
    // small default page size and our 30-page pagination cap silently
    // truncated long windows on busy orgs. Pinning to 31 (the documented
    // max) ensures a 90-day window completes in <= 3 pages, matching the
    // manual reconciliation query verified against a real invoice.
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    await getAnthropicCosts(7);
    const url = (fetchSpy.mock.calls[0][0] as string);
    expect(url).toContain('limit=31');
  });

  it('normalizes model name by stripping version date suffix', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [{
        starting_at: '2026-04-20T00:00:00Z',
        ending_at: '2026-04-21T00:00:00Z',
        results: [
          { currency: 'USD', amount: '5.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-haiku-4-5-20251001', token_type: 'output_tokens' },
          { currency: 'USD', amount: '3.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-haiku-4-5-20251001', token_type: 'output_tokens' },
        ],
      }],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    const r = await getAnthropicCosts(90);
    expect(Object.keys(r![0].by_model)).toEqual(['claude-haiku-4-5']);
    // Raw (5.00 + 3.00) / 100 = 0.08
    expect(r![0].by_model['claude-haiku-4-5']).toBeCloseTo(0.08, 4);
  });
});

// Tests for the DB-backed persistent cache (Layer 2). These exercise the
// pool-aware path that lets historical days survive a server restart so
// only recent days (within RECENT_REFRESH_DAYS) trigger an API call.
describe('getAnthropicCosts (DB-backed cache)', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  // Build a minimal fake Pool whose query() reads from a queue of canned
  // results. Each call shifts one off so tests can assert call ordering.
  function makeMockPool(responses: Array<{ rows: any[] }>) {
    const calls: Array<{ text: string; values: any[] | undefined }> = [];
    const pool: any = {
      query: vi.fn(async (text: string, values?: any[]) => {
        calls.push({ text, values });
        return responses.shift() || { rows: [] };
      }),
    };
    return { pool, calls };
  }

  // Build a today-relative ISO date string (UTC) for `daysAgo` days back.
  function dateAgo(daysAgo: number): string {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - daysAgo);
    return d.toISOString().slice(0, 10);
  }

  beforeEach(() => {
    process.env.ANTHROPIC_ADMIN_KEY = FAKE_ADMIN_KEY;
    clearCache();
    fetchSpy = vi.spyOn(global, 'fetch') as unknown as ReturnType<typeof vi.spyOn>;
  });

  afterEach(() => {
    fetchSpy.mockRestore();
    if (ORIGINAL_KEY === undefined) {
      delete process.env.ANTHROPIC_ADMIN_KEY;
    } else {
      process.env.ANTHROPIC_ADMIN_KEY = ORIGINAL_KEY;
    }
    clearCache();
  });

  it('serves from DB without API call when full coverage AND recent rows are fresh', async () => {
    // Build a row for every day in the last 90 days, all fetched_at=now
    const now = new Date().toISOString();
    const rows = [];
    for (let i = 0; i < 90; i++) {
      rows.push({
        date: dateAgo(i),
        total_usd: 1.5,
        by_model: { 'claude-opus-4-6': 1.5 },
        fetched_at: now,
      });
    }
    const { pool, calls } = makeMockPool([{ rows }]);

    const result = await getAnthropicCosts(7, pool);

    expect(result).not.toBeNull();
    expect(result!.length).toBe(7); // filtered to 7d
    expect(fetchSpy).not.toHaveBeenCalled(); // no API call
    expect(calls.length).toBe(1); // exactly one DB read
    expect(calls[0].text).toContain('FROM platform_costs');
    expect(calls[0].text).toContain("source = $1");
    // 3 params: source, days-1, earliest-date floor
    expect(calls[0].values![0]).toBe('anthropic_api');
    expect(calls[0].values![1]).toBe(89);
    expect(calls[0].values![2]).toBe('2026-03-15');
  });

  it('hits the API when DB is empty', async () => {
    const { pool, calls } = makeMockPool([
      { rows: [] }, // initial load: empty
      { rows: [] }, // upsert response (we don't care)
    ]);

    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [{
        starting_at: dateAgo(1) + 'T00:00:00Z',
        ending_at: dateAgo(0) + 'T00:00:00Z',
        results: [
          { currency: 'USD', amount: '500.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' },
        ],
      }],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    const result = await getAnthropicCosts(7, pool);

    expect(result).not.toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    // 2 DB calls: load + upsert
    expect(calls.length).toBe(2);
    expect(calls[1].text).toContain('INSERT INTO platform_costs');
    expect(calls[1].text).toContain('ON CONFLICT (date, source)');
  });

  it('hits the API when recent rows are stale (fetched on a prior UTC day)', async () => {
    // Full 90d coverage but recent days fetched 2 days ago (stale by-day)
    const oldFetchedAt = new Date();
    oldFetchedAt.setUTCDate(oldFetchedAt.getUTCDate() - 2);
    const rows = [];
    for (let i = 0; i < 90; i++) {
      rows.push({
        date: dateAgo(i),
        total_usd: 1.0,
        by_model: { 'claude-opus-4-6': 1.0 },
        fetched_at: oldFetchedAt.toISOString(),
      });
    }
    const { pool } = makeMockPool([{ rows }, { rows: [] }]);

    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [{
        starting_at: dateAgo(0) + 'T00:00:00Z',
        ending_at: dateAgo(0) + 'T00:00:00Z',
        results: [{ currency: 'USD', amount: '700.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
      }],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    await getAnthropicCosts(7, pool);

    // Stale recent rows must trigger the API path
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('hits the API when one or more dates in the window are missing', async () => {
    // Coverage for 89 of 90 days; one missing day in the middle
    const now = new Date().toISOString();
    const rows = [];
    for (let i = 0; i < 90; i++) {
      if (i === 30) continue; // skip one
      rows.push({
        date: dateAgo(i),
        total_usd: 1.0,
        by_model: {},
        fetched_at: now,
      });
    }
    const { pool } = makeMockPool([{ rows }, { rows: [] }]);

    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    await getAnthropicCosts(7, pool);

    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('serves DB rows when API fails (no rebuild attempt during cooldown)', async () => {
    // DB has older rows but recent stale, API will fail
    const oldFetchedAt = new Date();
    oldFetchedAt.setUTCDate(oldFetchedAt.getUTCDate() - 5);
    const rows = [];
    for (let i = 0; i < 90; i++) {
      rows.push({
        date: dateAgo(i),
        total_usd: 5.0,
        by_model: {},
        fetched_at: oldFetchedAt.toISOString(),
      });
    }
    const { pool } = makeMockPool([{ rows }]);

    fetchSpy.mockResolvedValueOnce(new Response('Forbidden', { status: 403 }));

    const result = await getAnthropicCosts(7, pool);

    // We get the stale DB rows back, not null
    expect(result).not.toBeNull();
    expect(result!.length).toBe(7);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('falls back to in-memory cache without DB query on hot path', async () => {
    const now = new Date().toISOString();
    const rows = [];
    for (let i = 0; i < 90; i++) {
      rows.push({ date: dateAgo(i), total_usd: 2.0, by_model: {}, fetched_at: now });
    }
    const { pool, calls } = makeMockPool([{ rows }]);

    // First call: cold; loads from DB.
    await getAnthropicCosts(7, pool);
    expect(calls.length).toBe(1);

    // Second call: hot; should NOT touch DB at all.
    await getAnthropicCosts(30, pool);
    expect(calls.length).toBe(1); // unchanged
  });

  it('rebuildAnthropicCostsCache deletes anthropic_api rows then refetches', async () => {
    const { rebuildAnthropicCostsCache } = await import('../../src/services/anthropic-costs');
    const { pool, calls } = makeMockPool([
      { rows: [] }, // DELETE response
      { rows: [] }, // upsert response
    ]);

    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify({
      data: [{
        starting_at: dateAgo(1) + 'T00:00:00Z',
        ending_at: dateAgo(0) + 'T00:00:00Z',
        results: [{ currency: 'USD', amount: '900.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
      }],
      has_more: false,
      next_page: null,
    }), { status: 200 }));

    const result = await rebuildAnthropicCostsCache(pool);

    expect(result.success).toBe(true);
    expect(result.rowsFetched).toBe(1);
    // First DB call must be DELETE on the anthropic_api source.
    expect(calls[0].text).toContain('DELETE FROM platform_costs');
    expect(calls[0].text).toContain('source = $1');
    expect(calls[0].values).toEqual(['anthropic_api']);
    // Second DB call must be the upsert.
    expect(calls[1].text).toContain('INSERT INTO platform_costs');
  });

  it('rebuildAnthropicCostsCache returns failure when API call fails', async () => {
    const { rebuildAnthropicCostsCache } = await import('../../src/services/anthropic-costs');
    const { pool } = makeMockPool([{ rows: [] }]); // DELETE ok

    fetchSpy.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));

    const result = await rebuildAnthropicCostsCache(pool);

    expect(result.success).toBe(false);
    expect(result.error).toBe('API fetch failed');
    expect(result.rowsFetched).toBe(0);
  });

  it('rebuildAnthropicCostsCache returns failure when ANTHROPIC_ADMIN_KEY unset', async () => {
    const { rebuildAnthropicCostsCache } = await import('../../src/services/anthropic-costs');
    delete process.env.ANTHROPIC_ADMIN_KEY;
    const { pool, calls } = makeMockPool([]);

    const result = await rebuildAnthropicCostsCache(pool);

    expect(result.success).toBe(false);
    expect(result.error).toContain('ANTHROPIC_ADMIN_KEY');
    expect(calls.length).toBe(0); // no DB ops attempted
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('upsertApiCosts builds parameterized multi-row INSERT', async () => {
    const { __test } = await import('../../src/services/anthropic-costs');
    const { pool, calls } = makeMockPool([{ rows: [] }]);

    await __test.upsertApiCosts(pool as any, [
      { date: '2026-04-29', total_usd: 12.34, by_model: { 'claude-opus-4-6': 12.34 } },
      { date: '2026-04-30', total_usd: 5.0, by_model: { 'claude-haiku-4-5': 5.0 } },
    ]);

    expect(calls.length).toBe(1);
    expect(calls[0].text).toContain('INSERT INTO platform_costs');
    expect(calls[0].text).toContain('ON CONFLICT (date, source)');
    // Two rows, three params each.
    expect(calls[0].values!.length).toBe(6);
    expect(calls[0].values![0]).toBe('2026-04-29');
    expect(calls[0].values![1]).toBe(12.34);
    // by_model serialized as JSON
    expect(JSON.parse(calls[0].values![2] as string)).toEqual({ 'claude-opus-4-6': 12.34 });
  });

  it('upsertApiCosts is a no-op for empty rows', async () => {
    const { __test } = await import('../../src/services/anthropic-costs');
    const { pool, calls } = makeMockPool([]);

    await __test.upsertApiCosts(pool as any, []);

    expect(calls.length).toBe(0); // no SQL fired
  });
});

// Pure-function tests for the date-helper logic.
describe('anthropic-costs internal helpers', () => {
  // Same Date-only freeze as above: the getAnthropicCostsAndStatus tests mock
  // buckets hardcoded to 2026-04-2x, which age out of the trailing 90-day
  // window under the real clock. Pure-function tests pass `now` explicitly,
  // so the freeze is harmless for them.
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-04-27T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('windowHasMissingDates flags missing dates inside the window', async () => {
    const { windowHasMissingDates } = await import('../../src/services/anthropic-costs');
    const now = Date.UTC(2026, 3, 30); // 2026-04-30
    expect(windowHasMissingDates(new Set(), 3, now)).toBe(true);
    expect(windowHasMissingDates(new Set(['2026-04-30', '2026-04-29', '2026-04-28']), 3, now)).toBe(false);
    expect(windowHasMissingDates(new Set(['2026-04-30', '2026-04-29']), 3, now)).toBe(true);
  });

  it('windowHasMissingDates ignores dates earlier than EARLIEST_DATA_DATE', async () => {
    const { windowHasMissingDates } = await import('../../src/services/anthropic-costs');
    // 90d window from 2026-04-30 reaches back to 2026-01-31; the floor is
    // 2026-03-15. Even with NO cached rows for Jan/Feb, the function should
    // ONLY report missing if rows >= 2026-03-15 are missing.
    const now = Date.UTC(2026, 3, 30); // 2026-04-30
    // Build a set covering 2026-03-15 through 2026-04-30 (every day on/after the floor)
    const allOnOrAfterFloor = new Set<string>();
    const floor = new Date(Date.UTC(2026, 2, 15)); // 2026-03-15
    for (let d = new Date(floor); d <= new Date(now); d.setUTCDate(d.getUTCDate() + 1)) {
      allOnOrAfterFloor.add(d.toISOString().slice(0, 10));
    }
    // 90d window with all on-or-after-floor dates present, no pre-floor dates: no missing
    expect(windowHasMissingDates(allOnOrAfterFloor, 90, now)).toBe(false);

    // Drop one date >= floor: now missing
    const minusOne = new Set(allOnOrAfterFloor);
    minusOne.delete('2026-04-15');
    expect(windowHasMissingDates(minusOne, 90, now)).toBe(true);
  });

  it('recentRowsAreStale flags rows fetched on a prior UTC day', async () => {
    const { recentRowsAreStale } = await import('../../src/services/anthropic-costs');
    const now = Date.UTC(2026, 3, 30, 12, 0, 0); // 2026-04-30 noon UTC
    const today = '2026-04-30';
    const yesterday = '2026-04-29';
    const dayBefore = '2026-04-28';
    const todayIso = new Date(now).toISOString();
    const yesterdayIso = new Date(now - 86400_000).toISOString();

    // All fresh
    expect(recentRowsAreStale(
      [
        { date: today, total_usd: 0, by_model: {}, fetched_at: todayIso },
        { date: yesterday, total_usd: 0, by_model: {}, fetched_at: todayIso },
        { date: dayBefore, total_usd: 0, by_model: {}, fetched_at: todayIso },
      ],
      3,
      now
    )).toBe(false);

    // One stale
    expect(recentRowsAreStale(
      [
        { date: today, total_usd: 0, by_model: {}, fetched_at: todayIso },
        { date: yesterday, total_usd: 0, by_model: {}, fetched_at: yesterdayIso },
        { date: dayBefore, total_usd: 0, by_model: {}, fetched_at: todayIso },
      ],
      3,
      now
    )).toBe(true);

    // Missing one
    expect(recentRowsAreStale(
      [
        { date: today, total_usd: 0, by_model: {}, fetched_at: todayIso },
        { date: dayBefore, total_usd: 0, by_model: {}, fetched_at: todayIso },
      ],
      3,
      now
    )).toBe(true);

    // Null fetched_at counts as stale
    expect(recentRowsAreStale(
      [
        { date: today, total_usd: 0, by_model: {}, fetched_at: null },
        { date: yesterday, total_usd: 0, by_model: {}, fetched_at: todayIso },
        { date: dayBefore, total_usd: 0, by_model: {}, fetched_at: todayIso },
      ],
      3,
      now
    )).toBe(true);
  });

  it('getAnthropicCostsAndStatus: returns partial=true with reason citing the failing page on mid-pagination 500', async () => {
    // Regression guard for bug-ops-api-costs-endpoint-underreports
    // (2026-05-18): when page N of the cost_report stream fails, the
    // response must (a) preserve the buckets we DID collect AND (b) tell
    // the caller that the data is incomplete plus *why* so the UI / ops
    // API never shows a silently truncated period total as if it were
    // authoritative.
    const fetchSpy = vi.spyOn(global, 'fetch') as unknown as ReturnType<typeof vi.spyOn>;
    try {
      process.env.ANTHROPIC_ADMIN_KEY = FAKE_ADMIN_KEY;
      clearCache();

      const page1 = new Response(JSON.stringify({
        data: [{
          starting_at: '2026-04-20T00:00:00Z',
          ending_at: '2026-04-21T00:00:00Z',
          results: [{ currency: 'USD', amount: '500.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
        }],
        has_more: true,
        next_page: 'cursor-page-2',
      }), { status: 200 });
      const page2 = new Response(JSON.stringify({
        data: [{
          starting_at: '2026-04-21T00:00:00Z',
          ending_at: '2026-04-22T00:00:00Z',
          results: [{ currency: 'USD', amount: '700.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
        }],
        has_more: true,
        next_page: 'cursor-page-3',
      }), { status: 200 });
      const page3 = new Response('Internal Server Error', { status: 500 });

      fetchSpy
        .mockResolvedValueOnce(page1)
        .mockResolvedValueOnce(page2)
        .mockResolvedValueOnce(page3);

      const result = await getAnthropicCostsAndStatus(90);

      // Rows from pages 1 + 2 are preserved.
      expect(result.rows).not.toBeNull();
      expect(result.rows!.map(r => r.date).sort()).toEqual(['2026-04-20', '2026-04-21']);
      // Partial flag with reason naming the failing page + status.
      expect(result.partial).toBe(true);
      expect(result.partialReason).toMatch(/mid_pagination_error/);
      expect(result.partialReason).toContain('500');
      expect(result.partialReason).toContain('page_3');
    } finally {
      fetchSpy.mockRestore();
      delete process.env.ANTHROPIC_ADMIN_KEY;
      clearCache();
    }
  });

  it('getAnthropicCostsAndStatus: returns partial=false on full multi-page success', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch') as unknown as ReturnType<typeof vi.spyOn>;
    try {
      process.env.ANTHROPIC_ADMIN_KEY = FAKE_ADMIN_KEY;
      clearCache();

      const page1 = new Response(JSON.stringify({
        data: [{
          starting_at: '2026-04-20T00:00:00Z',
          ending_at: '2026-04-21T00:00:00Z',
          results: [{ currency: 'USD', amount: '100.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
        }],
        has_more: true,
        next_page: 'cursor-page-2',
      }), { status: 200 });
      const page2 = new Response(JSON.stringify({
        data: [{
          starting_at: '2026-04-21T00:00:00Z',
          ending_at: '2026-04-22T00:00:00Z',
          results: [{ currency: 'USD', amount: '200.00', workspace_id: null, description: 'd', cost_type: 'tokens', model: 'claude-opus-4-6', token_type: 'output_tokens' }],
        }],
        has_more: false,
        next_page: null,
      }), { status: 200 });

      fetchSpy.mockResolvedValueOnce(page1).mockResolvedValueOnce(page2);

      const result = await getAnthropicCostsAndStatus(90);

      expect(result.rows).not.toBeNull();
      expect(result.rows!.length).toBe(2);
      expect(result.partial).toBe(false);
      expect(result.partialReason).toBeNull();
    } finally {
      fetchSpy.mockRestore();
      delete process.env.ANTHROPIC_ADMIN_KEY;
      clearCache();
    }
  });

  it('getAnthropicCostsAndStatus: returns rows=null with no partial reason when first page fails', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch') as unknown as ReturnType<typeof vi.spyOn>;
    try {
      process.env.ANTHROPIC_ADMIN_KEY = FAKE_ADMIN_KEY;
      clearCache();

      fetchSpy.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));

      const result = await getAnthropicCostsAndStatus(7);
      expect(result.rows).toBeNull();
      // No buckets collected = full failure, not partial.
      expect(result.partial).toBe(false);
      expect(result.partialReason).toBeNull();
    } finally {
      fetchSpy.mockRestore();
      delete process.env.ANTHROPIC_ADMIN_KEY;
      clearCache();
    }
  });

  it('mergeRows: API rows win on collision; cached-only rows pass through', async () => {
    const { mergeRows } = await import('../../src/services/anthropic-costs');
    const cached = [
      { date: '2026-04-29', total_usd: 1.0, by_model: { 'claude-opus-4-6': 1.0 }, fetched_at: null },
      { date: '2026-04-28', total_usd: 2.0, by_model: { 'claude-haiku-4-5': 2.0 }, fetched_at: null },
    ];
    const fresh = [
      { date: '2026-04-29', total_usd: 99.0, by_model: { 'claude-opus-4-6': 99.0 } },
      { date: '2026-04-30', total_usd: 5.0, by_model: { 'claude-opus-4-6': 5.0 } },
    ];
    const result = mergeRows(cached, fresh);
    // Sorted desc by date
    expect(result.map(r => r.date)).toEqual(['2026-04-30', '2026-04-29', '2026-04-28']);
    // API row wins for 2026-04-29
    expect(result[1].total_usd).toBe(99.0);
    // Cached-only 2026-04-28 passes through
    expect(result[2].total_usd).toBe(2.0);
  });
});
