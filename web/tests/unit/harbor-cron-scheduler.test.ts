// tests/unit/harbor-cron-scheduler.test.ts
// Verifies the four Harbor self-learn cron sweeps are wired into the
// boot path when CAS_HARBOR_ENABLED=true, and skipped when not.
//
// Each per-scheduler test asserts setInterval is called (so the periodic
// loop is registered) and a NodeJS.Timeout is returned. The cron run
// functions are mocked so the deferred first-run setTimeout cannot make
// real DB calls if it ever fires before test teardown clears it.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn(),
    on: vi.fn(),
  },
}));

vi.mock('../../src/cron/topic_kb_sweep', () => ({
  runTopicKbSweep: vi.fn().mockResolvedValue({
    windowDays: 7,
    turnsScanned: 0,
    clustersExtracted: 0,
    insertedCount: 0,
    skippedDuplicateCount: 0,
    skippedInvalidCount: 0,
    totalCostMicros: 0,
    inserted: [],
  }),
}));

vi.mock('../../src/cron/memory_low_utility_sweep', () => ({
  runMemoryLowUtilitySweep: vi.fn().mockResolvedValue({
    staleDays: 14,
    minMemoryRows: 20,
    eligibleScopes: 0,
    rowsFlagged: 0,
  }),
}));

vi.mock('../../src/cron/rail_flag_sweep', () => ({
  runRailFlagSweep: vi.fn().mockResolvedValue({
    windowDays: 7,
    totalTurnsScanned: 0,
    populationFindings: 0,
    perScopeFindings: 0,
    findingsRecorded: 0,
    findings: [],
  }),
}));

vi.mock('../../src/cron/persona_learning_digest', () => ({
  runPersonaLearningDigest: vi.fn().mockResolvedValue({
    windowDays: 30,
    totalCandidates: 0,
    surfacedCount: 0,
    byCategory: {},
    emailed: false,
    skippedReason: 'no_candidates',
  }),
  PERSONA_DIGEST_CATEGORIES: [
    'harbor_persona',
    'archetype_correction',
    'harbor_extraction',
  ],
}));

import {
  startHarborCronSchedulers,
  startTopicKbSweepScheduler,
  startMemoryLowUtilitySweepScheduler,
  startRailFlagSweepScheduler,
  startPersonaLearningDigestScheduler,
} from '../../src/services/harbor-cron-scheduler';

let originalHarborFlag: string | undefined;
const createdIntervals: NodeJS.Timeout[] = [];

beforeEach(() => {
  originalHarborFlag = process.env.CAS_HARBOR_ENABLED;
  createdIntervals.length = 0;
  vi.clearAllMocks();
});

afterEach(() => {
  for (const t of createdIntervals) {
    clearInterval(t);
  }
  createdIntervals.length = 0;
  if (originalHarborFlag === undefined) {
    delete process.env.CAS_HARBOR_ENABLED;
  } else {
    process.env.CAS_HARBOR_ENABLED = originalHarborFlag;
  }
});

describe('startHarborCronSchedulers', () => {
  it('starts no schedulers when CAS_HARBOR_ENABLED is unset', () => {
    delete process.env.CAS_HARBOR_ENABLED;
    const setIntervalSpy = vi.spyOn(global, 'setInterval');

    const intervals = startHarborCronSchedulers();

    expect(intervals.topicKb).toBeUndefined();
    expect(intervals.memoryLowUtility).toBeUndefined();
    expect(intervals.railFlag).toBeUndefined();
    expect(intervals.personaLearningDigest).toBeUndefined();
    expect(setIntervalSpy).not.toHaveBeenCalled();
  });

  it('starts no schedulers when CAS_HARBOR_ENABLED is "false"', () => {
    process.env.CAS_HARBOR_ENABLED = 'false';
    const setIntervalSpy = vi.spyOn(global, 'setInterval');

    const intervals = startHarborCronSchedulers();

    expect(intervals.topicKb).toBeUndefined();
    expect(intervals.memoryLowUtility).toBeUndefined();
    expect(intervals.railFlag).toBeUndefined();
    expect(intervals.personaLearningDigest).toBeUndefined();
    expect(setIntervalSpy).not.toHaveBeenCalled();
  });

  it('starts all 4 schedulers when CAS_HARBOR_ENABLED=true', () => {
    process.env.CAS_HARBOR_ENABLED = 'true';
    const setIntervalSpy = vi.spyOn(global, 'setInterval');

    const intervals = startHarborCronSchedulers();

    expect(intervals.topicKb).toBeDefined();
    expect(intervals.memoryLowUtility).toBeDefined();
    expect(intervals.railFlag).toBeDefined();
    expect(intervals.personaLearningDigest).toBeDefined();
    expect(setIntervalSpy).toHaveBeenCalledTimes(4);

    if (intervals.topicKb) createdIntervals.push(intervals.topicKb);
    if (intervals.memoryLowUtility) createdIntervals.push(intervals.memoryLowUtility);
    if (intervals.railFlag) createdIntervals.push(intervals.railFlag);
    if (intervals.personaLearningDigest) createdIntervals.push(intervals.personaLearningDigest);
  });
});

describe('startTopicKbSweepScheduler', () => {
  it('registers a setInterval and returns the handle', () => {
    const setIntervalSpy = vi.spyOn(global, 'setInterval');

    const handle = startTopicKbSweepScheduler(60_000);
    createdIntervals.push(handle);

    expect(handle).toBeDefined();
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    expect(setIntervalSpy.mock.calls[0][1]).toBe(60_000);
  });
});

describe('startMemoryLowUtilitySweepScheduler', () => {
  it('registers a setInterval and returns the handle', () => {
    const setIntervalSpy = vi.spyOn(global, 'setInterval');

    const handle = startMemoryLowUtilitySweepScheduler(60_000);
    createdIntervals.push(handle);

    expect(handle).toBeDefined();
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    expect(setIntervalSpy.mock.calls[0][1]).toBe(60_000);
  });
});

describe('startRailFlagSweepScheduler', () => {
  it('registers a setInterval and returns the handle', () => {
    const setIntervalSpy = vi.spyOn(global, 'setInterval');

    const handle = startRailFlagSweepScheduler(60_000);
    createdIntervals.push(handle);

    expect(handle).toBeDefined();
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    expect(setIntervalSpy.mock.calls[0][1]).toBe(60_000);
  });
});

describe('startPersonaLearningDigestScheduler', () => {
  it('registers a setInterval and returns the handle', () => {
    const setIntervalSpy = vi.spyOn(global, 'setInterval');

    const handle = startPersonaLearningDigestScheduler(60_000);
    createdIntervals.push(handle);

    expect(handle).toBeDefined();
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    expect(setIntervalSpy.mock.calls[0][1]).toBe(60_000);
  });
});

describe('resolveIntervalMs (R1-W2 review)', () => {
  it('falls back to the default for NaN, empty, zero, negative and sub-minute values', async () => {
    const { resolveIntervalMs } = await import('../../src/services/harbor-cron-scheduler');
    const WEEK = 7 * 24 * 60 * 60 * 1000;
    for (const raw of [undefined, '', 'abc', '7d', '0', '-5', '999', '59999']) {
      expect(resolveIntervalMs(raw, WEEK), String(raw)).toBe(WEEK);
    }
  });

  it('accepts a sensible override', async () => {
    const { resolveIntervalMs } = await import('../../src/services/harbor-cron-scheduler');
    expect(resolveIntervalMs('3600000', 1)).toBe(3_600_000);
    expect(resolveIntervalMs('60000', 1)).toBe(60_000);
  });

  it('a garbage env value cannot turn the scheduler into a ~1ms hot loop: it checks hourly for a weekly sweep', async () => {
    const { SWEEP_CHECK_EVERY_MS } = await import('../../src/services/harbor-cron-scheduler');
    const saved = process.env.RAIL_FLAG_SWEEP_INTERVAL_MS;
    process.env.RAIL_FLAG_SWEEP_INTERVAL_MS = 'not-a-number';
    const setIntervalSpy = vi.spyOn(global, 'setInterval');
    try {
      const handle = startRailFlagSweepScheduler();
      createdIntervals.push(handle);
      // The timer is the check, not the sweep: once an hour it asks the
      // database whether the weekly run is due.
      expect(SWEEP_CHECK_EVERY_MS).toBe(60 * 60 * 1000);
      expect(setIntervalSpy.mock.calls[0][1]).toBe(SWEEP_CHECK_EVERY_MS);
    } finally {
      if (saved === undefined) delete process.env.RAIL_FLAG_SWEEP_INTERVAL_MS;
      else process.env.RAIL_FLAG_SWEEP_INTERVAL_MS = saved;
    }
  });
});

describe('weekly means weekly: a sweep runs when the database says a run is due, not when the process starts', () => {
  const WEEK = 7 * 24 * 60 * 60 * 1000;

  async function mocks() {
    const pool = (await import('../../src/db/index')).default as unknown as { query: ReturnType<typeof vi.fn> };
    const sweep = (await import('../../src/cron/rail_flag_sweep')).runRailFlagSweep as unknown as ReturnType<typeof vi.fn>;
    return { poolQuery: pool.query, sweep };
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('a boot inside the week (the claim is refused) runs nothing: not at 60 seconds, not at the hourly checks', async () => {
    const { poolQuery, sweep } = await mocks();
    poolQuery.mockResolvedValue({ rows: [] }); // a run was claimed less than an interval ago
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    createdIntervals.push(startRailFlagSweepScheduler());

    await vi.advanceTimersByTimeAsync(60_000);
    expect(poolQuery).toHaveBeenCalledTimes(1);
    expect(sweep).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(3 * 60 * 60 * 1000);
    expect(poolQuery.mock.calls.length).toBeGreaterThanOrEqual(4);
    expect(sweep).not.toHaveBeenCalled();
    log.mockRestore();
  });

  it('when the claim is granted the sweep runs once, and the claim names the sweep and its interval', async () => {
    const { poolQuery, sweep } = await mocks();
    poolQuery.mockResolvedValueOnce({ rows: [{ key: 'harbor_sweep_last_run:rail-flag-sweep' }] }); // due
    poolQuery.mockResolvedValue({ rows: [] }); // and then not again
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    createdIntervals.push(startRailFlagSweepScheduler());

    await vi.advanceTimersByTimeAsync(60_000);
    expect(sweep).toHaveBeenCalledTimes(1);
    const [sql, params] = poolQuery.mock.calls[0];
    expect(String(sql)).toMatch(/INSERT INTO platform_metadata/);
    expect(String(sql)).toMatch(/ON CONFLICT \(key\) DO UPDATE/);
    expect(String(sql)).toMatch(/WHERE platform_metadata\.updated_at <= NOW\(\) - \(\$2::double precision \* INTERVAL '1 millisecond'\)/);
    expect(params).toEqual(['harbor_sweep_last_run:rail-flag-sweep', WEEK]);

    await vi.advanceTimersByTimeAsync(2 * 60 * 60 * 1000);
    expect(sweep).toHaveBeenCalledTimes(1);
    log.mockRestore();
  });

  it('when the claim cannot be read or written the sweep does not run, and the failure is logged', async () => {
    const { poolQuery, sweep } = await mocks();
    poolQuery.mockRejectedValue(new Error('relation "platform_metadata" does not exist'));
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    createdIntervals.push(startRailFlagSweepScheduler());

    await vi.advanceTimersByTimeAsync(60_000);
    expect(sweep).not.toHaveBeenCalled();
    expect(error.mock.calls.map((call) => call.join(' ')).join('\n')).toMatch(
      /\[rail-flag-sweep\] could not check whether a run is due; not running: relation "platform_metadata" does not exist/
    );
    log.mockRestore();
    error.mockRestore();
  });

  it('an interval shorter than an hour is checked at that interval', () => {
    const setIntervalSpy = vi.spyOn(global, 'setInterval');
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    createdIntervals.push(startRailFlagSweepScheduler(5 * 60_000));
    expect(setIntervalSpy.mock.calls[0][1]).toBe(5 * 60_000);
    log.mockRestore();
  });
});

describe('guardedRun (R1-W2 review)', () => {
  it('drops an overlapping invocation and runs again once the first finishes', async () => {
    const { guardedRun } = await import('../../src/services/harbor-cron-scheduler');
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const body = vi.fn(async () => {
      await gate;
    });
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const run = guardedRun('test-sweep', body);

    const first = run();
    await run(); // overlaps the first: skipped
    expect(body).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('previous run still in progress'));

    release();
    await first;
    await run();
    expect(body).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });

  it('releases the guard when the body throws', async () => {
    const { guardedRun } = await import('../../src/services/harbor-cron-scheduler');
    const body = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(undefined);
    const run = guardedRun('test-sweep', body);
    await expect(run()).rejects.toThrow('boom');
    await run();
    expect(body).toHaveBeenCalledTimes(2);
  });
});
