// tests/unit/email-scheduler.test.ts
// Unit tests for src/services/email-scheduler.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the database pool
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
    },
  };
});

// Mock the email module
vi.mock('../../src/services/email', () => ({
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
}));

import pool from '../../src/db/index';
import {
  scheduleWelcomeSequence,
  schedulePostBuildSequence,
  cancelWelcomeSequence,
  processDueEmails,
} from '../../src/services/email-scheduler';
import {
  sendWelcomeHowItWorksEmail,
  sendWelcomeUseCasesEmail,
  sendWelcomeBetaUrgencyEmail,
  sendPostBuildCheckInEmail,
  sendPostBuildTestimonialEmail,
} from '../../src/services/email';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockQuery.mockReset();
  vi.clearAllMocks();
});

// --- scheduleWelcomeSequence ---

describe('scheduleWelcomeSequence', () => {
  it('inserts 3 scheduled emails when no existing sequence found', async () => {
    // Check for existing: none found
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT 3 rows
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await scheduleWelcomeSequence('customer-uuid-1');

    expect(mockQuery).toHaveBeenCalledTimes(2);
    const insertCall = mockQuery.mock.calls[1];
    expect(insertCall[0]).toContain('welcome_how_it_works');
    expect(insertCall[0]).toContain('welcome_use_cases');
    expect(insertCall[0]).toContain('welcome_beta_urgency');
    expect(insertCall[1]).toEqual(['customer-uuid-1']);
  });

  it('does not insert if welcome sequence already exists', async () => {
    // Check for existing: found one
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'existing-id' }] });

    await scheduleWelcomeSequence('customer-uuid-1');

    expect(mockQuery).toHaveBeenCalledTimes(1);
  });

  it('handles database errors gracefully', async () => {
    mockQuery.mockRejectedValueOnce(new Error('DB connection lost'));

    // Should not throw
    await scheduleWelcomeSequence('customer-uuid-1');
  });
});

// --- schedulePostBuildSequence ---

describe('schedulePostBuildSequence', () => {
  it('inserts 2 scheduled emails with context', async () => {
    // Check for existing: none
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await schedulePostBuildSequence('customer-uuid-2', 'MyApp', 'https://myapp.up.railway.app');

    expect(mockQuery).toHaveBeenCalledTimes(2);
    const insertCall = mockQuery.mock.calls[1];
    expect(insertCall[0]).toContain('post_build_check_in');
    expect(insertCall[0]).toContain('post_build_testimonial');
    // Context JSON should include app_name and live_url
    const context = JSON.parse(insertCall[1][1]);
    expect(context.app_name).toBe('MyApp');
    expect(context.live_url).toBe('https://myapp.up.railway.app');
  });

  it('does not insert if post-build sequence already exists for this app', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'existing-id' }] });

    await schedulePostBuildSequence('customer-uuid-2', 'MyApp', 'https://myapp.up.railway.app');

    expect(mockQuery).toHaveBeenCalledTimes(1);
  });
});

// --- cancelWelcomeSequence ---

describe('cancelWelcomeSequence', () => {
  it('cancels pending welcome emails and returns count', async () => {
    mockQuery.mockResolvedValueOnce({ rowCount: 3 });

    const count = await cancelWelcomeSequence('customer-uuid-3');

    expect(count).toBe(3);
    const sql = mockQuery.mock.calls[0][0] as string;
    expect(sql).toContain('cancelled_at = NOW()');
    expect(sql).toContain('welcome_how_it_works');
  });

  it('returns 0 when no emails to cancel', async () => {
    mockQuery.mockResolvedValueOnce({ rowCount: 0 });

    const count = await cancelWelcomeSequence('customer-uuid-4');
    expect(count).toBe(0);
  });

  it('returns 0 on error', async () => {
    mockQuery.mockRejectedValueOnce(new Error('DB error'));

    const count = await cancelWelcomeSequence('customer-uuid-5');
    expect(count).toBe(0);
  });
});

// --- processDueEmails ---

describe('processDueEmails', () => {
  it('sends welcome_how_it_works email and marks as sent', async () => {
    // One atomic claim query returns the due rows (sent_at already stamped).
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'sched-1',
        template: 'welcome_how_it_works',
        context: {},
        email: 'alice@example.com',
        name: 'Alice',
      }],
    });

    const count = await processDueEmails();

    expect(count).toBe(1);
    expect(sendWelcomeHowItWorksEmail).toHaveBeenCalledWith('alice@example.com', 'Alice');
    // The claim is the only query: there is no separate mark-as-sent step that
    // two processes could both reach for the same row.
    expect(mockQuery).toHaveBeenCalledTimes(1);
  });

  it('claims rows atomically: FOR UPDATE SKIP LOCKED and sent_at in ONE statement', async () => {
    // Regression: the old code ran SELECT ... FOR UPDATE SKIP LOCKED through
    // pool.query() outside a transaction, so the lock was released immediately
    // and two instances both sent the same nurture email.
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await processDueEmails();

    const sql = mockQuery.mock.calls[0][0] as string;
    expect(sql).toMatch(/FOR UPDATE SKIP LOCKED/);
    expect(sql).toMatch(/UPDATE email_schedule[\s\S]*SET sent_at = NOW\(\)/);
    expect(sql).toMatch(/WITH due AS/);
    expect(sql).toMatch(/cancelled_at IS NULL/);
  });

  it('sends welcome_use_cases email', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'sched-2',
        template: 'welcome_use_cases',
        context: {},
        email: 'bob@example.com',
        name: 'Bob',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await processDueEmails();

    expect(sendWelcomeUseCasesEmail).toHaveBeenCalledWith('bob@example.com', 'Bob');
  });

  it('sends welcome_beta_urgency email', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'sched-3',
        template: 'welcome_beta_urgency',
        context: {},
        email: 'carol@example.com',
        name: 'Carol',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await processDueEmails();

    expect(sendWelcomeBetaUrgencyEmail).toHaveBeenCalledWith('carol@example.com', 'Carol');
  });

  it('sends post_build_check_in email with context', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'sched-4',
        template: 'post_build_check_in',
        context: { app_name: 'MyApp', live_url: 'https://myapp.up.railway.app' },
        email: 'dave@example.com',
        name: 'Dave',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await processDueEmails();

    expect(sendPostBuildCheckInEmail).toHaveBeenCalledWith(
      'dave@example.com', 'Dave', 'MyApp', 'https://myapp.up.railway.app'
    );
  });

  it('sends post_build_testimonial email with context', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'sched-5',
        template: 'post_build_testimonial',
        context: { app_name: 'CoolApp', live_url: 'https://coolapp.up.railway.app' },
        email: 'eve@example.com',
        name: 'Eve',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await processDueEmails();

    expect(sendPostBuildTestimonialEmail).toHaveBeenCalledWith(
      'eve@example.com', 'Eve', 'CoolApp'
    );
  });

  it('cancels emails with unknown template', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'sched-unknown',
        template: 'nonexistent_template',
        context: {},
        email: 'test@example.com',
        name: 'Test',
      }],
    });
    // cancel query
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const count = await processDueEmails();

    expect(count).toBe(0);
    expect(mockQuery.mock.calls[1][0]).toContain('cancelled_at = NOW()');
    // The claim is undone so the row does not look "sent".
    expect(mockQuery.mock.calls[1][0]).toContain('sent_at = NULL');
  });

  it('processes multiple due emails in one batch', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'sched-a',
          template: 'welcome_how_it_works',
          context: {},
          email: 'a@example.com',
          name: 'A',
        },
        {
          id: 'sched-b',
          template: 'welcome_use_cases',
          context: {},
          email: 'b@example.com',
          name: 'B',
        },
      ],
    });
    // Two UPDATE sent_at calls
    mockQuery.mockResolvedValue({ rows: [] });

    const count = await processDueEmails();

    expect(count).toBe(2);
    expect(sendWelcomeHowItWorksEmail).toHaveBeenCalledWith('a@example.com', 'A');
    expect(sendWelcomeUseCasesEmail).toHaveBeenCalledWith('b@example.com', 'B');
  });

  it('returns 0 when no due emails', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const count = await processDueEmails();
    expect(count).toBe(0);
  });

  it('releases the claim when email sending throws so the next tick retries', async () => {
    const sendFn = sendWelcomeHowItWorksEmail as ReturnType<typeof vi.fn>;
    sendFn.mockRejectedValueOnce(new Error('SMTP failure'));

    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'sched-fail',
        template: 'welcome_how_it_works',
        context: {},
        email: 'fail@example.com',
        name: 'Fail',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // release

    const count = await processDueEmails();

    expect(count).toBe(0);
    expect(mockQuery).toHaveBeenCalledTimes(2); // claim + release
    expect(mockQuery.mock.calls[1][0]).toContain('sent_at = NULL');
    expect(mockQuery.mock.calls[1][1]).toEqual(['sched-fail']);
  });

  it('two overlapping runs cannot both send the same row (second claim returns nothing)', async () => {
    const row = {
      id: 'sched-race', template: 'welcome_use_cases', context: {}, email: 'r@example.com', name: 'R',
    };
    // Postgres hands the row to exactly one of two concurrent claim statements.
    mockQuery.mockResolvedValueOnce({ rows: [row] }).mockResolvedValueOnce({ rows: [] });

    const [a, b] = await Promise.all([processDueEmails(), processDueEmails()]);

    expect(a + b).toBe(1);
    expect(sendWelcomeUseCasesEmail).toHaveBeenCalledTimes(1);
  });

  it('handles database errors gracefully', async () => {
    mockQuery.mockRejectedValueOnce(new Error('Connection timeout'));

    const count = await processDueEmails();
    expect(count).toBe(0);
  });
});

// --- Nurture email template tests (verify they exist and are callable) ---

describe('nurture email templates', () => {
  it('exports sendWelcomeHowItWorksEmail', () => {
    expect(typeof sendWelcomeHowItWorksEmail).toBe('function');
  });

  it('exports sendWelcomeUseCasesEmail', () => {
    expect(typeof sendWelcomeUseCasesEmail).toBe('function');
  });

  it('exports sendWelcomeBetaUrgencyEmail', () => {
    expect(typeof sendWelcomeBetaUrgencyEmail).toBe('function');
  });

  it('exports sendPostBuildCheckInEmail', () => {
    expect(typeof sendPostBuildCheckInEmail).toBe('function');
  });

  it('exports sendPostBuildTestimonialEmail', () => {
    expect(typeof sendPostBuildTestimonialEmail).toBe('function');
  });
});
