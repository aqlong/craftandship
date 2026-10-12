import { describe, it, expect, vi, beforeEach } from 'vitest';
import request from 'supertest';
import app from '../../src/app';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
    },
  };
});

vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendPreviewReadyEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildQueuedEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn((email: string) => `token-for-${email}`),
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
vi.mock('../../src/services/stripe', () => ({
  constructWebhookEvent: vi.fn(),
}));
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello!'),
  processFeatureMessage: vi.fn(),
  generateFeatureGreeting: vi.fn().mockResolvedValue('Hello!'),
}));

import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
const mockQuery = pool.query as ReturnType<typeof vi.fn>;

// The guide form carries the CSRF field like every other form, and the route
// checks it.
const csrf = (): string => generateCsrfToken({} as Request);

describe('POST /guide (newsletter signup persist)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
  });

  it('INSERTs the trimmed + lowercased email with ON CONFLICT', async () => {
    const res = await request(app)
      .post('/guide')
      .type('form')
      .send({ _csrf: csrf(), email: '  PERSON@Example.COM  ' });

    expect(res.status).toBe(200);
    const insertCall = mockQuery.mock.calls.find(
      ([sql]) => typeof sql === 'string' && sql.includes('INSERT INTO newsletter_subscribers')
    );
    expect(insertCall).toBeDefined();
    const [sql, params] = insertCall as [string, unknown[]];
    expect(sql).toMatch(/ON CONFLICT \(email\)/);
    expect(sql).toMatch(/DO UPDATE/);
    expect(sql).toMatch(/unsubscribed_at IS NULL/);
    expect(params[0]).toBe('person@example.com');
    expect(params[1]).toBe('guide');
  });

  it('is idempotent: second POST with same email still resolves and re-INSERTs (DB handles no-op)', async () => {
    await request(app).post('/guide').type('form').send({ _csrf: csrf(), email: 'dup@example.com' });
    await request(app).post('/guide').type('form').send({ _csrf: csrf(), email: 'dup@example.com' });

    const insertCalls = mockQuery.mock.calls.filter(
      ([sql]) => typeof sql === 'string' && sql.includes('INSERT INTO newsletter_subscribers')
    );
    expect(insertCalls).toHaveLength(2);
    expect(insertCalls.every(([, params]) => (params as unknown[])[0] === 'dup@example.com')).toBe(true);
  });

  it('rejects invalid email without touching the DB', async () => {
    const res = await request(app)
      .post('/guide')
      .type('form')
      .send({ _csrf: csrf(), email: 'not-an-email' });

    expect(res.status).toBe(200);
    const insertCalls = mockQuery.mock.calls.filter(
      ([sql]) => typeof sql === 'string' && sql.includes('INSERT INTO newsletter_subscribers')
    );
    expect(insertCalls).toHaveLength(0);
  });

  it('a stored address gets the thank-you, and the log line that says it was recorded', async () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => undefined);
    try {
      const res = await request(app).post('/guide').type('form').send({ _csrf: csrf(), email: 'stored@example.com' });
      expect(res.status).toBe(200);
      expect(res.text).toContain('Your address is on the list.');
      expect(infoSpy.mock.calls.some((call) => String(call[0]).includes('subscriber recorded'))).toBe(true);
    } finally {
      infoSpy.mockRestore();
    }
  });

  it('an address the database did not take is not thanked: 503, the reason, the typed address kept, and no "recorded" log line', async () => {
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => undefined);
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      mockQuery.mockRejectedValueOnce(new Error('db down'));
      const res = await request(app)
        .post('/guide')
        .type('form')
        .send({ _csrf: csrf(), email: 'Unlucky@Example.com' });
      expect(res.status).toBe(503);
      expect(res.text).toContain('Your address could not be added to the list just now.');
      expect(res.text).not.toContain('Your address is on the list.');
      expect(res.text).not.toContain('Thank you.');
      // The form is shown again with what was typed, and the guide is still one click away.
      expect(res.text).toContain('value="unlucky@example.com"');
      expect(res.text).toContain('href="/guide/read"');
      expect(infoSpy.mock.calls.some((call) => String(call[0]).includes('subscriber recorded'))).toBe(false);
      expect(errorSpy.mock.calls.some((call) => String(call[0]).includes('db insert failed'))).toBe(true);
    } finally {
      infoSpy.mockRestore();
      errorSpy.mockRestore();
    }
  });

  it('requires the CSRF token the form carries', async () => {
    const res = await request(app).post('/guide').type('form').send({ email: 'person@example.com' });
    expect(res.status).toBe(403);
    expect(mockQuery).not.toHaveBeenCalled();
  });
});
