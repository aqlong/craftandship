import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the db pool before importing the service under test
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
    },
  };
});

import {
  createAnonDraft,
  getAnonDraftByBuildId,
  claimAnonDraft,
  claimAnonDraftBySessionId,
  generateAnonSessionId,
} from '../../src/services/anon-intake';
import pool from '../../src/db/index';

const mockPool = pool as unknown as { query: ReturnType<typeof vi.fn> };

const VALID_SESSION = 'a'.repeat(64);
const BUILD_ID = '11111111-1111-1111-1111-111111111111';
const CUSTOMER_ID = '22222222-2222-2222-2222-222222222222';

beforeEach(() => {
  mockPool.query.mockReset();
});

describe('generateAnonSessionId', () => {
  it('returns a 64-char hex string', () => {
    const id = generateAnonSessionId();
    expect(id).toMatch(/^[0-9a-f]{64}$/);
  });

  it('returns unique values on each call', () => {
    const a = generateAnonSessionId();
    const b = generateAnonSessionId();
    expect(a).not.toBe(b);
  });
});

describe('createAnonDraft', () => {
  it('inserts a new build row with customer_id NULL and returns the build ID', async () => {
    mockPool.query.mockResolvedValueOnce({ rows: [{ id: BUILD_ID }], rowCount: 1 });

    const result = await createAnonDraft(VALID_SESSION);

    expect(result).toEqual({ buildId: BUILD_ID });
    expect(mockPool.query).toHaveBeenCalledTimes(1);
    const [sql, params] = mockPool.query.mock.calls[0];
    expect(sql).toMatch(/INSERT INTO builds/);
    // The draft has no owner: customer_id is a literal NULL, the session id is the only handle.
    expect(sql).toMatch(/VALUES\s*\(\s*NULL\s*,/);
    expect(params).toEqual([VALID_SESSION]);
  });

  it('rejects an empty or short session ID without hitting the DB', async () => {
    await expect(createAnonDraft('')).rejects.toThrow('Invalid anonymous session ID');
    await expect(createAnonDraft('short')).rejects.toThrow('Invalid anonymous session ID');
    expect(mockPool.query).not.toHaveBeenCalled();
  });
});

describe('getAnonDraftByBuildId', () => {
  it('returns the draft row when customer_id IS NULL and session matches', async () => {
    const row = {
      id: BUILD_ID,
      anonymous_session_id: VALID_SESSION,
      customer_id: null,
      status: 'intake',
      tier: 'build',
      created_at: new Date('2026-04-05T00:00:00Z'),
    };
    mockPool.query.mockResolvedValueOnce({ rows: [row], rowCount: 1 });

    const result = await getAnonDraftByBuildId(BUILD_ID, VALID_SESSION);

    expect(result).toEqual(row);
    const [sql, params] = mockPool.query.mock.calls[0];
    expect(sql).toMatch(/customer_id IS NULL/);
    expect(sql).toMatch(/anonymous_session_id = \$2/);
    expect(params).toEqual([BUILD_ID, VALID_SESSION]);
  });

  it('returns null when session ID does not match', async () => {
    mockPool.query.mockResolvedValueOnce({ rows: [], rowCount: 0 });
    const result = await getAnonDraftByBuildId(BUILD_ID, 'wrong-session');
    expect(result).toBeNull();
  });

  it('returns null without querying when buildId or sessionId is empty', async () => {
    expect(await getAnonDraftByBuildId('', VALID_SESSION)).toBeNull();
    expect(await getAnonDraftByBuildId(BUILD_ID, '')).toBeNull();
    expect(mockPool.query).not.toHaveBeenCalled();
  });
});

describe('claimAnonDraft', () => {
  it('returns true when exactly one unclaimed row matching session is updated', async () => {
    mockPool.query.mockResolvedValueOnce({ rows: [{ id: BUILD_ID }], rowCount: 1 });

    const result = await claimAnonDraft(BUILD_ID, VALID_SESSION, CUSTOMER_ID);

    expect(result).toBe(true);
    const [sql, params] = mockPool.query.mock.calls[0];
    expect(sql).toMatch(/UPDATE builds/);
    expect(sql).toMatch(/SET customer_id = \$1, anonymous_session_id = NULL/);
    expect(sql).toMatch(/WHERE id = \$2/);
    expect(sql).toMatch(/customer_id IS NULL/);
    expect(sql).toMatch(/anonymous_session_id = \$3/);
    expect(params).toEqual([CUSTOMER_ID, BUILD_ID, VALID_SESSION]);
  });

  it('returns false when the draft is already claimed (zero rows updated)', async () => {
    mockPool.query.mockResolvedValueOnce({ rows: [], rowCount: 0 });
    const result = await claimAnonDraft(BUILD_ID, VALID_SESSION, CUSTOMER_ID);
    expect(result).toBe(false);
  });

  it('returns false without querying when any argument is empty', async () => {
    expect(await claimAnonDraft('', VALID_SESSION, CUSTOMER_ID)).toBe(false);
    expect(await claimAnonDraft(BUILD_ID, '', CUSTOMER_ID)).toBe(false);
    expect(await claimAnonDraft(BUILD_ID, VALID_SESSION, '')).toBe(false);
    expect(mockPool.query).not.toHaveBeenCalled();
  });
});

describe('claimAnonDraftBySessionId', () => {
  it('claims the newest unclaimed draft for the session and returns its build ID', async () => {
    mockPool.query.mockResolvedValueOnce({ rows: [{ id: BUILD_ID }], rowCount: 1 });

    const result = await claimAnonDraftBySessionId(VALID_SESSION, CUSTOMER_ID);

    expect(result).toEqual({ claimed: true, buildId: BUILD_ID });
    const [sql, params] = mockPool.query.mock.calls[0];
    expect(sql).toMatch(/UPDATE builds/);
    expect(sql).toMatch(/ORDER BY created_at DESC/);
    expect(sql).toMatch(/LIMIT 1/);
    expect(params).toEqual([CUSTOMER_ID, VALID_SESSION]);
  });

  it('returns claimed=false when no draft matches the session', async () => {
    mockPool.query.mockResolvedValueOnce({ rows: [], rowCount: 0 });
    const result = await claimAnonDraftBySessionId(VALID_SESSION, CUSTOMER_ID);
    expect(result).toEqual({ claimed: false, buildId: null });
  });

  it('returns claimed=false without querying when sessionId or customerId is empty', async () => {
    expect(await claimAnonDraftBySessionId('', CUSTOMER_ID)).toEqual({ claimed: false, buildId: null });
    expect(await claimAnonDraftBySessionId(VALID_SESSION, '')).toEqual({ claimed: false, buildId: null });
    expect(mockPool.query).not.toHaveBeenCalled();
  });
});
