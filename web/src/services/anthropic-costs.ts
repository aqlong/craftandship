// Anthropic Admin API cost fetcher with two-layer cache:
//
//   Layer 1: in-memory (10-min TTL) for the same-process hot path.
//   Layer 2: platform_costs DB rows (source='anthropic_api') survive restarts.
//
// On call:
//   1. Fast path, serve from in-memory cache if fresh.
//   2. Otherwise, load DB rows for the requested window.
//   3. If DB has full coverage AND no recent rows are stale (older than
//      today UTC), serve from DB without an API call.
//   4. Otherwise hit the API once for the full 90d window (the API has no
//      per-day endpoint; bucket_width=1d returns all days), UPSERT new rows
//      into platform_costs, merge, return.
//
// Historical days never need re-fetching after they settle, so a server
// restart only triggers an API call for the recent days, not the full 90.

import type { Pool } from 'pg';

const CACHE_TTL_MS = 10 * 60 * 1000; // 10 minutes
const FAILURE_COOLDOWN_MS = 60 * 1000; // 60s quiet period after exhausted retries
const API_BASE = 'https://api.anthropic.com/v1/organizations';
const MAX_FETCH_DAYS = 90;
// How many trailing days are considered "may still change" and re-fetched
// once per UTC day even if a DB row exists. Anthropic billing settles
// within a few days; 3 covers the typical lag while keeping API calls minimal.
const RECENT_REFRESH_DAYS = 3;
const CACHE_SOURCE = 'anthropic_api';
// Hard floor for the API fetch + missing-date check. The Anthropic Admin
// API has no usage rows for the founder's organization before this date,
// so anything earlier is empty buckets only. Override via env if needed.
const EARLIEST_DATA_DATE = process.env.CAS_ANTHROPIC_EARLIEST_DATE || '2026-03-15';

interface CostResult {
  currency: string;
  amount: string;
  workspace_id: string | null;
  description: string;
  cost_type: string;
  model?: string;
  token_type?: string;
}

interface CostBucket {
  starting_at: string;
  ending_at: string;
  results: CostResult[];
}

interface CostResponse {
  data: CostBucket[];
  has_more: boolean;
  next_page: string | null;
}

export interface DailyCostFromApi {
  date: string;
  total_usd: number;
  by_model: Record<string, number>;
}

// Result wrapper that carries partial-data context alongside the rows so
// callers (and the API surface) can distinguish a complete fetch from one
// that succeeded partially (mid-pagination error, pagination cap reached,
// or DB-cached rows served because the live API was unreachable). Without
// this, the UI silently renders a "false-low" number and the operator has
// no signal that platform spend is under-reported.
export interface AnthropicCostsResult {
  rows: DailyCostFromApi[] | null;
  partial: boolean;
  partialReason: string | null;
}

// Hard cap on cost_report pagination pages we'll follow in a single call.
// Anthropic supports limit=31 buckets per page, so 30 pages = ~930 days,
// well beyond the 90-day window we ever request.
const MAX_PAGINATION_PAGES = 30;
// Per-page bucket limit; matches the Admin API max so each page returns up
// to 31 daily buckets. Without an explicit limit, the default page size is
// small (~7) and combined with our 30-page cap was leaving long windows
// silently incomplete on busy orgs.
const PAGE_BUCKET_LIMIT = 31;
// Rate-limit handling: only HTTP 429 is retried (exponential backoff starting
// at 2s). Other failures end the fetch and the caller serves the DB cache.
const MAX_RATE_LIMIT_ATTEMPTS = 4;
const MAX_RETRY_WAIT_MS = 30_000;

let _cache: {
  data: DailyCostFromApi[];
  fetchedAt: number;
  partial: boolean;
  partialReason: string | null;
} | null = null;
let _failedAt: number | null = null;

function filterToRange(data: DailyCostFromApi[], days: number): DailyCostFromApi[] {
  const cutoff = new Date();
  cutoff.setUTCDate(cutoff.getUTCDate() - (days - 1));
  const cutoffStr = cutoff.toISOString().slice(0, 10);
  return data.filter(d => d.date >= cutoffStr);
}

interface CachedRow {
  date: string;
  total_usd: number;
  by_model: Record<string, number>;
  fetched_at: string | null;
}

async function loadCachedCosts(pool: Pool, days: number): Promise<CachedRow[]> {
  const result = await pool.query(
    `SELECT date::text AS date,
            total_usd::float AS total_usd,
            by_model,
            fetched_at::text AS fetched_at
       FROM platform_costs
      WHERE source = $1
        AND date >= GREATEST(
              CURRENT_DATE - $2::int * INTERVAL '1 day',
              $3::date
            )
      ORDER BY date DESC`,
    [CACHE_SOURCE, days - 1, EARLIEST_DATA_DATE]
  );
  return result.rows.map((r: { date: string; total_usd: number; by_model: Record<string, number> | null; fetched_at: string | null }) => ({
    date: r.date,
    total_usd: Number(r.total_usd) || 0,
    by_model: r.by_model || {},
    fetched_at: r.fetched_at,
  }));
}

async function upsertApiCosts(pool: Pool, rows: DailyCostFromApi[]): Promise<void> {
  if (rows.length === 0) return;
  const values: string[] = [];
  const params: unknown[] = [];
  rows.forEach((r, i) => {
    const base = i * 3;
    values.push(`($${base + 1}::date, $${base + 2}::numeric, $${base + 3}::jsonb)`);
    params.push(r.date, r.total_usd, JSON.stringify(r.by_model));
  });
  const sql =
    `INSERT INTO platform_costs (date, total_usd, by_model, source, fetched_at)
     SELECT v.date, v.total_usd, v.by_model, '${CACHE_SOURCE}', NOW()
       FROM (VALUES ${values.join(', ')}) AS v(date, total_usd, by_model)
     ON CONFLICT (date, source)
       DO UPDATE SET total_usd  = EXCLUDED.total_usd,
                     by_model   = EXCLUDED.by_model,
                     fetched_at = NOW()`;
  await pool.query(sql, params);
}

// True if the recent-refresh window has any missing or stale-by-day row.
// Stale = fetched_at's UTC date != today's UTC date.
export function recentRowsAreStale(
  rows: CachedRow[],
  recentRefreshDays: number,
  nowMs: number
): boolean {
  if (recentRefreshDays <= 0) return false;
  const recent: string[] = [];
  for (let i = recentRefreshDays - 1; i >= 0; i--) {
    const d = new Date(nowMs);
    d.setUTCDate(d.getUTCDate() - i);
    recent.push(d.toISOString().slice(0, 10));
  }
  const todayUtc = new Date(nowMs).toISOString().slice(0, 10);
  for (const d of recent) {
    const row = rows.find(r => r.date === d);
    if (!row) return true; // missing entirely
    if (!row.fetched_at) return true;
    const fetchedDay = new Date(row.fetched_at).toISOString().slice(0, 10);
    if (fetchedDay !== todayUtc) return true;
  }
  return false;
}

// True if any date in the trailing-windowDays window AND >= EARLIEST_DATA_DATE
// is absent from dbDates. Dates earlier than EARLIEST_DATA_DATE are skipped
// because the Anthropic API has no usage data for them, so a "missing" row
// there would never be filled by an API fetch (just empty buckets).
export function windowHasMissingDates(
  dbDates: Set<string>,
  windowDays: number,
  nowMs: number
): boolean {
  for (let i = windowDays - 1; i >= 0; i--) {
    const d = new Date(nowMs);
    d.setUTCDate(d.getUTCDate() - i);
    const dStr = d.toISOString().slice(0, 10);
    if (dStr < EARLIEST_DATA_DATE) continue;
    if (!dbDates.has(dStr)) return true;
  }
  return false;
}

async function fetchFromAnthropicApi(
  adminKey: string
): Promise<{ rows: DailyCostFromApi[] | null; partial: boolean; partialReason: string | null }> {
  try {
    const endDate = new Date();
    endDate.setUTCDate(endDate.getUTCDate() + 1); // tomorrow to include today
    const startDate = new Date();
    startDate.setUTCDate(startDate.getUTCDate() - (MAX_FETCH_DAYS - 1));

    // Clamp the start to EARLIEST_DATA_DATE so we never request empty
    // pre-history buckets. Saves API time and pages.
    let startStr = startDate.toISOString().slice(0, 10);
    if (startStr < EARLIEST_DATA_DATE) startStr = EARLIEST_DATA_DATE;
    startStr += 'T00:00:00Z';
    const endStr = endDate.toISOString().slice(0, 10) + 'T00:00:00Z';

    // The Admin API's default page size is small (observed ~7 buckets) and
    // without an explicit limit, requesting a 90d window with bucket_width=1d
    // could silently truncate behind our 30-page cap on busy orgs. Pin to the
    // documented max (31) so a 90-day window completes in <= 3 pages and
    // matches the manual reconciliation query verified 2026-05-16.
    const url = `${API_BASE}/cost_report?starting_at=${startStr}&ending_at=${endStr}&bucket_width=1d&limit=${PAGE_BUCKET_LIMIT}`;

    const allBuckets: CostBucket[] = [];
    let nextPage: string | null = null;
    let pageCount = 0;
    let fetchFailed = false;
    let failedStatus = 0;
    let failedPage = 0;

    do {
      // The page token is opaque server-issued text; encode it so characters
      // such as '+', '/' or '=' cannot corrupt the query string.
      const fetchUrl = nextPage ? `${url}&page=${encodeURIComponent(nextPage)}` : url;
      let resp: Response | null = null;
      let retryDelay = 2000;
      for (let attempt = 0; attempt < MAX_RATE_LIMIT_ATTEMPTS; attempt++) {
        resp = await fetch(fetchUrl, {
          headers: {
            'anthropic-version': '2023-06-01',
            'x-api-key': adminKey,
            'User-Agent': 'CraftAndShip/1.0',
          },
          signal: AbortSignal.timeout(10000),
        });
        if (resp.status !== 429) break;
        // No point sleeping after the final attempt: we are about to give up.
        if (attempt === MAX_RATE_LIMIT_ATTEMPTS - 1) break;
        // Honour Retry-After, but never block the admin page for more than
        // MAX_RETRY_WAIT_MS on a header we do not control.
        const retryAfter = parseInt(resp.headers.get('retry-after') || '0', 10);
        const wait = Math.min(retryAfter > 0 ? retryAfter * 1000 : retryDelay, MAX_RETRY_WAIT_MS);
        console.warn(
          'anthropic-costs: 429 rate limit on page %d, waiting %dms (attempt %d)',
          pageCount + 1,
          wait,
          attempt + 1
        );
        await new Promise(r => setTimeout(r, wait));
        retryDelay *= 2;
      }

      if (!resp || !resp.ok) {
        const bodyHint =
          resp?.status === 429 ? 'rate limit exhausted' : '(body unavailable after retry)';
        console.error('anthropic-costs: API returned %d: %s', resp?.status, bodyHint);
        fetchFailed = true;
        failedStatus = resp?.status ?? 0;
        failedPage = pageCount + 1;
        break;
      }

      const json = (await resp.json()) as CostResponse;
      allBuckets.push(...json.data);
      nextPage = json.has_more ? json.next_page : null;
      pageCount++;
    } while (nextPage && pageCount < MAX_PAGINATION_PAGES);

    if (fetchFailed && allBuckets.length === 0) {
      return { rows: null, partial: false, partialReason: null };
    }

    // Detect "pagination cap reached but more data exists." has_more was
    // still true when we hit MAX_PAGINATION_PAGES, so the result is partial
    // even though no individual page failed.
    const paginationCapReached = !fetchFailed && nextPage !== null && pageCount >= MAX_PAGINATION_PAGES;

    let partial = false;
    let partialReason: string | null = null;
    if (fetchFailed) {
      partial = true;
      partialReason = `mid_pagination_error_status_${failedStatus}_on_page_${failedPage}_of_at_least_${pageCount + 1}`;
      console.warn(
        'anthropic-costs: partial data (%d buckets, %d pages) due to mid-pagination error: %s',
        allBuckets.length,
        pageCount,
        partialReason
      );
    } else if (paginationCapReached) {
      partial = true;
      partialReason = `pagination_cap_reached_at_${pageCount}_pages_more_available`;
      console.warn('anthropic-costs: %s', partialReason);
    }

    const dailyMap = new Map<string, DailyCostFromApi>();
    for (const bucket of allBuckets) {
      const date = bucket.starting_at.slice(0, 10);
      if (!dailyMap.has(date)) {
        dailyMap.set(date, { date, total_usd: 0, by_model: {} });
      }
      const day = dailyMap.get(date)!;
      for (const r of bucket.results) {
        // The Cost Report API returns `amount` as a decimal string in the
        // currency's lowest unit (cents for USD), not in dollars. Divide by 100
        // exactly once, here; every figure downstream (platform_costs.total_usd,
        // by_model, the dashboard) is already in dollars.
        const amountUsd = (parseFloat(r.amount) || 0) / 100;
        day.total_usd += amountUsd;
        if (r.model) {
          const model = r.model.replace(/-20\d{6}$/, '');
          day.by_model[model] = (day.by_model[model] || 0) + amountUsd;
        }
      }
    }

    const rows = Array.from(dailyMap.values()).sort((a, b) => b.date.localeCompare(a.date));
    return { rows, partial, partialReason };
  } catch (err) {
    console.error('anthropic-costs: Error fetching from Anthropic API:', (err as Error).message);
    return { rows: null, partial: false, partialReason: null };
  }
}

// Merge cached DB rows with fresh API rows. API rows win on date collision.
export function mergeRows(cached: CachedRow[], fresh: DailyCostFromApi[]): DailyCostFromApi[] {
  const byDate = new Map<string, DailyCostFromApi>();
  for (const c of cached) {
    byDate.set(c.date, { date: c.date, total_usd: c.total_usd, by_model: c.by_model });
  }
  for (const f of fresh) {
    byDate.set(f.date, f);
  }
  return Array.from(byDate.values()).sort((a, b) => b.date.localeCompare(a.date));
}

// Same call shape as getAnthropicCosts but exposes the partial-data context
// so the UI / API response can flag incomplete data instead of silently
// rendering a false-low number.
export async function getAnthropicCostsAndStatus(
  days: number,
  pool?: Pool
): Promise<AnthropicCostsResult> {
  const adminKey = process.env.ANTHROPIC_ADMIN_KEY;

  // 1. In-memory hot path. Carry the cached partial flag forward so a
  //    partial fetch isn't laundered into "complete" by a subsequent
  //    in-window read.
  if (_cache && Date.now() - _cache.fetchedAt < CACHE_TTL_MS) {
    return {
      rows: filterToRange(_cache.data, days),
      partial: _cache.partial,
      partialReason: _cache.partialReason,
    };
  }

  // 2. Try DB cache first if pool is available
  let cachedRows: CachedRow[] = [];
  if (pool) {
    try {
      // Always load the full MAX window so we can serve any subrange
      // without an extra DB query.
      cachedRows = await loadCachedCosts(pool, MAX_FETCH_DAYS);
    } catch (err) {
      console.error('anthropic-costs: DB read failed:', (err as Error).message);
    }
  }

  const cachedDateSet = new Set(cachedRows.map(r => r.date));
  const nowMs = Date.now();
  const missingInWindow = windowHasMissingDates(cachedDateSet, MAX_FETCH_DAYS, nowMs);
  const recentStale = recentRowsAreStale(cachedRows, RECENT_REFRESH_DAYS, nowMs);
  const needsApi = missingInWindow || recentStale;

  // 3. Full coverage with fresh recent rows: serve from DB.
  if (!needsApi && cachedRows.length > 0) {
    const merged = mergeRows(cachedRows, []);
    _cache = { data: merged, fetchedAt: nowMs, partial: false, partialReason: null };
    return { rows: filterToRange(merged, days), partial: false, partialReason: null };
  }

  // 4. Need API. Bail out if no key set; serve DB rows if any.
  if (!adminKey) {
    if (cachedRows.length > 0) {
      const merged = mergeRows(cachedRows, []);
      return {
        rows: filterToRange(merged, days),
        partial: true,
        partialReason: 'admin_key_unset_serving_db_cache_only',
      };
    }
    return { rows: null, partial: false, partialReason: null };
  }

  // 5. Cooldown check, do not hammer Anthropic if we just exhausted retries.
  if (_failedAt !== null && nowMs - _failedAt < FAILURE_COOLDOWN_MS) {
    if (cachedRows.length > 0) {
      const merged = mergeRows(cachedRows, []);
      return {
        rows: filterToRange(merged, days),
        partial: true,
        partialReason: 'api_recent_failure_serving_db_cache_during_cooldown',
      };
    }
    return { rows: null, partial: false, partialReason: null };
  }

  // 6. Hit the API.
  const { rows: freshRows, partial: fetchPartial, partialReason: fetchPartialReason } =
    await fetchFromAnthropicApi(adminKey);

  if (freshRows === null) {
    _failedAt = Date.now();
    if (cachedRows.length > 0) {
      const merged = mergeRows(cachedRows, []);
      return {
        rows: filterToRange(merged, days),
        partial: true,
        partialReason: 'api_fetch_failed_serving_db_cache_only',
      };
    }
    return { rows: null, partial: false, partialReason: null };
  }

  _failedAt = null;

  // 7. Persist fresh rows to DB if pool available
  if (pool && freshRows.length > 0) {
    try {
      await upsertApiCosts(pool, freshRows);
    } catch (err) {
      console.error('anthropic-costs: DB write failed:', (err as Error).message);
    }
  }

  // 8. Merge + cache + return
  const merged = mergeRows(cachedRows, freshRows);
  _cache = {
    data: merged,
    fetchedAt: Date.now(),
    partial: fetchPartial,
    partialReason: fetchPartialReason,
  };
  return {
    rows: filterToRange(merged, days),
    partial: fetchPartial,
    partialReason: fetchPartialReason,
  };
}

// Backwards-compatible wrapper that drops the partial-data context. Most
// callers do not need it. Use getAnthropicCostsAndStatus when surfacing
// completeness to the UI or API response.
export async function getAnthropicCosts(
  days: number,
  pool?: Pool
): Promise<DailyCostFromApi[] | null> {
  const { rows } = await getAnthropicCostsAndStatus(days, pool);
  return rows;
}

// Force-rebuild: wipes the persistent cache (DB rows with our source) and
// the in-memory cache, then refetches from the API. Returns rowsFetched and
// success/error so the admin route can give the operator a clean signal.
export async function rebuildAnthropicCostsCache(
  pool: Pool
): Promise<{ rowsFetched: number; success: boolean; error?: string }> {
  const adminKey = process.env.ANTHROPIC_ADMIN_KEY;
  if (!adminKey) {
    return { rowsFetched: 0, success: false, error: 'ANTHROPIC_ADMIN_KEY not set' };
  }

  try {
    await pool.query(`DELETE FROM platform_costs WHERE source = $1`, [CACHE_SOURCE]);
  } catch (err) {
    return { rowsFetched: 0, success: false, error: (err as Error).message };
  }

  _cache = null;
  _failedAt = null;

  const { rows: freshRows, partial: fetchPartial, partialReason: fetchPartialReason } =
    await fetchFromAnthropicApi(adminKey);
  if (freshRows === null) {
    _failedAt = Date.now();
    return { rowsFetched: 0, success: false, error: 'API fetch failed' };
  }

  if (freshRows.length > 0) {
    try {
      await upsertApiCosts(pool, freshRows);
    } catch (err) {
      return { rowsFetched: 0, success: false, error: (err as Error).message };
    }
  }

  _cache = {
    data: freshRows,
    fetchedAt: Date.now(),
    partial: fetchPartial,
    partialReason: fetchPartialReason,
  };
  return { rowsFetched: freshRows.length, success: true };
}

// Export for testing
export function clearCache(): void {
  _cache = null;
  _failedAt = null;
}

// Export for testing
export const __test = {
  loadCachedCosts,
  upsertApiCosts,
  RECENT_REFRESH_DAYS,
  CACHE_SOURCE,
};
