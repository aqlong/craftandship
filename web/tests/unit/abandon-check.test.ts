import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const { mockClientQuery, mockClientRelease } = vi.hoisted(() => ({
  mockClientQuery: vi.fn(),
  mockClientRelease: vi.fn(),
}));

// Mock DB pool
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
      connect: vi.fn().mockResolvedValue({
        query: mockClientQuery,
        release: mockClientRelease,
      }),
    },
  };
});

// Mock email service
vi.mock('../../src/services/email', () => ({
  sendAbandonNudgeEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn().mockReturnValue('mock-token'),
  escapeHtml: vi.fn((s: string) => s),
}));

import pool from '../../src/db/index';
import { sendAbandonNudgeEmail } from '../../src/services/email';
import { checkAbandonedIntakes, resumeIfAbandoned, startAbandonCheck } from '../../src/services/abandon-check';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const mockSendEmail = sendAbandonNudgeEmail as ReturnType<typeof vi.fn>;

describe('checkAbandonedIntakes', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockSendEmail.mockReset();
    mockClientQuery.mockReset();
    mockClientRelease.mockReset();
    mockSendEmail.mockResolvedValue(undefined);
  });

  describe('detection (24h)', () => {
    it('marks intake builds as abandoned after 24h of inactivity', async () => {
      // Detection query returns one stale build
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'build-1', customer_id: 'cust-1', email: 'test@example.com', name: 'Test User' }],
      });
      // UPDATE status to abandoned
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });
      // Cleanup query returns no old builds
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const result = await checkAbandonedIntakes();

      expect(result.marked).toBe(1);
      expect(mockQuery.mock.calls[1][0]).toContain("status = 'abandoned'");
      expect(mockQuery.mock.calls[1][1]).toEqual(['build-1']);
    });

    it('does not nudge when another instance already moved the build (status guard)', async () => {
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'build-race', customer_id: 'cust-1', email: 'race@example.com', name: 'Race' }],
      });
      // UPDATE ... WHERE status = 'intake' matches nothing: someone else won.
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 0 });
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const result = await checkAbandonedIntakes();

      expect(result.marked).toBe(0);
      expect(mockQuery.mock.calls[1][0]).toContain("status = 'intake'");
      expect(mockSendEmail).not.toHaveBeenCalled();
    });

    it('does not mark builds with recent messages', async () => {
      // Detection query returns empty
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // Cleanup query returns empty
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const result = await checkAbandonedIntakes();

      expect(result.marked).toBe(0);
      expect(mockSendEmail).not.toHaveBeenCalled();
    });

    it('sends nudge email with correct parameters', async () => {
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'build-abc', customer_id: 'cust-1', email: 'alice@example.com', name: 'Alice' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });
      mockQuery.mockResolvedValueOnce({ rows: [] });

      await checkAbandonedIntakes();

      expect(mockSendEmail).toHaveBeenCalledWith(
        'alice@example.com',
        'Alice',
        expect.stringContaining('/intake/build-abc')
      );
    });

    it('sets abandoned_at and abandoned_email_sent_at on marked builds', async () => {
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'build-1', customer_id: 'cust-1', email: 'test@example.com', name: 'Test' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });
      mockQuery.mockResolvedValueOnce({ rows: [] });

      await checkAbandonedIntakes();

      const updateSql = mockQuery.mock.calls[1][0] as string;
      expect(updateSql).toContain('abandoned_at');
      expect(updateSql).toContain('abandoned_email_sent_at');
    });
  });

  describe('cleanup (7 days)', () => {
    it('deletes builds abandoned for 7+ days with full cascade', async () => {
      // Detection returns empty
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // Cleanup returns old builds
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'old-build-1' }] });

      // Transaction queries
      mockClientQuery.mockResolvedValueOnce({}); // BEGIN
      mockClientQuery.mockResolvedValueOnce({}); // DELETE feature_messages
      mockClientQuery.mockResolvedValueOnce({}); // DELETE feature_requests
      mockClientQuery.mockResolvedValueOnce({}); // DELETE intake_messages
      mockClientQuery.mockResolvedValueOnce({}); // DELETE build_logs
      mockClientQuery.mockResolvedValueOnce({}); // DELETE dead_letter_builds
      mockClientQuery.mockResolvedValueOnce({}); // UPDATE parent_build_id
      mockClientQuery.mockResolvedValueOnce({ rowCount: 1 }); // DELETE builds
      mockClientQuery.mockResolvedValueOnce({}); // COMMIT

      const result = await checkAbandonedIntakes();

      expect(result.deleted).toBe(1);
      expect(mockClientQuery.mock.calls[0][0]).toBe('BEGIN');
      expect(mockClientQuery.mock.calls[1][0]).toContain('feature_messages');
      expect(mockClientQuery.mock.calls[2][0]).toContain('feature_requests');
      expect(mockClientQuery.mock.calls[3][0]).toContain('intake_messages');
      expect(mockClientQuery.mock.calls[4][0]).toContain('build_logs');
      expect(mockClientQuery.mock.calls[5][0]).toContain('dead_letter_builds');
      expect(mockClientQuery.mock.calls[6][0]).toContain('parent_build_id');
      expect(mockClientQuery.mock.calls[7][0]).toContain('DELETE FROM builds');
      expect(mockClientQuery.mock.calls[8][0]).toBe('COMMIT');
      expect(mockClientRelease).toHaveBeenCalled();
    });

    it('does not delete recently abandoned builds', async () => {
      mockQuery.mockResolvedValueOnce({ rows: [] }); // detection
      mockQuery.mockResolvedValueOnce({ rows: [] }); // cleanup

      const result = await checkAbandonedIntakes();

      expect(result.deleted).toBe(0);
      expect(mockClientQuery).not.toHaveBeenCalled();
    });

    it('rolls back on cascade error', async () => {
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'old-build-1' }] });

      mockClientQuery.mockResolvedValueOnce({}); // BEGIN
      mockClientQuery.mockRejectedValueOnce(new Error('delete failed')); // feature_messages fails
      mockClientQuery.mockResolvedValueOnce({}); // ROLLBACK

      const result = await checkAbandonedIntakes();

      expect(result.deleted).toBe(0);
      expect(mockClientQuery).toHaveBeenCalledWith('ROLLBACK');
      expect(mockClientRelease).toHaveBeenCalled();
    });
  });

  describe('error handling', () => {
    it('returns { marked: 0, deleted: 0, anonDeleted: 0 } on DB error without throwing', async () => {
      // Both the authenticated phase and the anon sweep call pool.query;
      // reject both so neither phase can progress.
      mockQuery.mockRejectedValue(new Error('connection refused'));

      const result = await checkAbandonedIntakes();

      expect(result).toEqual({ marked: 0, deleted: 0, anonDeleted: 0 });
    });
  });
});

describe('resumeIfAbandoned', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('flips abandoned build back to intake and returns true', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ status: 'abandoned' }] });
    mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });

    const result = await resumeIfAbandoned('build-1');

    expect(result).toBe(true);
    const updateSql = mockQuery.mock.calls[1][0] as string;
    expect(updateSql).toContain("status = 'intake'");
    expect(updateSql).toContain('abandoned_at = NULL');
    expect(updateSql).toContain('abandoned_email_sent_at = NULL');
  });

  it('returns false for non-abandoned build', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ status: 'intake' }] });

    const result = await resumeIfAbandoned('build-1');

    expect(result).toBe(false);
    expect(mockQuery).toHaveBeenCalledTimes(1);
  });

  it('returns false for non-existent build', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const result = await resumeIfAbandoned('nonexistent');

    expect(result).toBe(false);
  });
});

describe('startAbandonCheck', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns an interval handle', () => {
    // Mock checkAbandonedIntakes indirectly by mocking the pool query
    mockQuery.mockResolvedValue({ rows: [] });

    const handle = startAbandonCheck(60000);
    expect(handle).toBeDefined();
    clearInterval(handle);
  });
});
