// tests/unit/routes.password-reset.test.ts
// Tests for forgot-password, reset-password, and unsubscribe routes in auth.ts

import { describe, it, expect, vi, beforeEach } from 'vitest';
import request from 'supertest';
import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';

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

// Mock the email service to prevent real HTTP calls
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendPasswordResetEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildQueuedEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn((email: string) => `hmac-token-for-${email}`),
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

// Mock services that other routes depend on
vi.mock('../../src/services/stripe', () => ({
  constructWebhookEvent: vi.fn(),
}));
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello! Describe your app.'),
}));

import pool from '../../src/db/index';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

// --- GET /forgot-password ---

describe('GET /forgot-password', () => {
  it('returns 200 with the forgot password form', async () => {
    const res = await request(app).get('/forgot-password');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Forgot Password');
  });
});

// --- POST /forgot-password ---

describe('POST /forgot-password', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns neutral "check your inbox" message when email field is empty', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: csrf, email: '' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('check your inbox');
  });

  it('returns neutral "check your inbox" message for a non-existent email (no user existence leak)', async () => {
    // DB returns no customer
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: csrf, email: 'nobody@example.com' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('check your inbox');
    // Must not reveal whether user exists or not
    expect(res.text).not.toContain('not found');
    expect(res.text).not.toContain('does not exist');
  });

  it('creates a token in DB, fires email, and returns neutral message for a valid email', async () => {
    // The one statement found the (verified) account and inserted a token.
    mockQuery.mockResolvedValueOnce({ rows: [{ name: 'Alice', email_verified: true }] });

    const { sendPasswordResetEmail } = await import('../../src/services/email');
    (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mockClear();

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: csrf, email: 'alice@example.com' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('check your inbox');

    // Token was inserted, by the one statement
    expect(mockQuery).toHaveBeenCalledTimes(1);
    const insertCall = mockQuery.mock.calls[0];
    expect(insertCall[0]).toContain('INSERT INTO password_reset_tokens');

    // The mail starts once the response has gone out.
    await vi.waitFor(() => expect(sendPasswordResetEmail).toHaveBeenCalledTimes(1));
    expect(sendPasswordResetEmail).toHaveBeenCalledWith(
      'alice@example.com',
      'Alice',
      expect.stringContaining('/reset-password?token=')
    );

    // The link is proof of mailbox control: it goes out by email only.
    // Rendering it in the response would let anyone who types a victim's
    // address take over the account.
    expect(res.text).not.toContain('/reset-password?token=');
    expect(res.text).not.toContain('receive the email?');
  });

  it('greets an unverified account by no name: whoever created the row typed it', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ name: 'URGENT: pay at evil.example', email_verified: false }] });
    const { sendPasswordResetEmail } = await import('../../src/services/email');
    (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mockClear();

    await request(app).post('/forgot-password').type('form').send({ _csrf: getValidCsrf(), email: 'alice@example.com' });

    await vi.waitFor(() => expect(sendPasswordResetEmail).toHaveBeenCalledTimes(1));
    expect((sendPasswordResetEmail as ReturnType<typeof vi.fn>).mock.calls[0][1]).toBeNull();
  });

  it('runs the same one statement for a known address and an unknown one, and mails only after the response', async () => {
    const { sendPasswordResetEmail } = await import('../../src/services/email');
    const sequences: unknown[][] = [];
    // The order in which the page was rendered and the mail was started.
    const events: string[] = [];
    const render = app.render.bind(app);
    const renderSpy = vi.spyOn(app, 'render').mockImplementation(((...args: Parameters<typeof app.render>) => {
      events.push('render');
      return render(...args);
    }) as typeof app.render);
    try {
      for (const answer of [[{ name: 'Alice', email_verified: true }], []]) {
        mockQuery.mockReset();
        mockQuery.mockResolvedValueOnce({ rows: answer });
        (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mockClear();
        (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mockImplementationOnce(async () => {
          events.push('mail');
        });
        const res = await request(app).post('/forgot-password').type('form').send({ _csrf: getValidCsrf(), email: 'someone@example.com' });
        expect(res.status).toBe(200);
        sequences.push(mockQuery.mock.calls.map(([sql, params]) => [sql, (params as unknown[]).length, (params as unknown[])[0]]));
      }
      await vi.waitFor(() => expect(events).toContain('mail'));
    } finally {
      renderSpy.mockRestore();
    }
    // Known address: the page (the view, then the layout), then the mail. Unknown: the page only.
    expect(events.join(',')).toMatch(/^(render,)+mail(,render)+$/);
    expect(sequences[0]).toHaveLength(1);
    expect(sequences[1]).toEqual(sequences[0]);
    const sql = String((sequences[0][0] as unknown[])[0]);
    // It inserts only for an address that has an account, and returns the account only then.
    expect(sql).toMatch(/target AS \(\s*SELECT id, name, email_verified FROM customers WHERE email = \$1\s*\)/);
    expect(sql).toMatch(/INSERT INTO password_reset_tokens \(customer_id, token\)\s+SELECT id, \$2 FROM target/);
  });

  it('answers an existing account and an unknown one with the same page', async () => {
    // Strip the per-request values (CSRF tokens, CSP nonce) and compare.
    const normalize = (html: string): string =>
      html
        .replace(/[0-9a-f]{32}\.(?:[0-9a-f]{32}|nosession)\.[0-9a-f]{64}/g, 'CSRF')
        .replace(/nonce="[^"]*"/g, 'nonce=""');

    mockQuery.mockResolvedValueOnce({ rows: [{ name: 'Alice', email_verified: true }] });
    const known = await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'alice@example.com' });

    mockQuery.mockReset();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const unknown = await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'nobody@example.com' });

    expect(known.status).toBe(200);
    expect(unknown.status).toBe(200);
    expect(normalize(known.text)).toBe(normalize(unknown.text));
  });

  it('builds the emailed link from BASE_URL, not from the request Host header', async () => {
    const original = process.env.BASE_URL;
    process.env.BASE_URL = 'https://app.example.test';
    try {
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-1', name: 'Alice' }] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const { sendPasswordResetEmail } = await import('../../src/services/email');
      (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mockClear();

      await request(app)
        .post('/forgot-password')
        .set('Host', 'attacker.example.test')
        .type('form')
        .send({ _csrf: getValidCsrf(), email: 'alice@example.com' });

      await new Promise(r => setTimeout(r, 10));
      const url = (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mock.calls[0][2] as string;
      expect(url.startsWith('https://app.example.test/reset-password?token=')).toBe(true);
      expect(url).not.toContain('attacker.example.test');
    } finally {
      if (original === undefined) delete process.env.BASE_URL;
      else process.env.BASE_URL = original;
    }
  });

  it('never writes a malformed Host header into the emailed link', async () => {
    const original = process.env.BASE_URL;
    delete process.env.BASE_URL;
    try {
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-1', name: 'Alice' }] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const { sendPasswordResetEmail } = await import('../../src/services/email');
      (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mockClear();

      await request(app)
        .post('/forgot-password')
        .set('Host', 'evil.example.test"><img src=x>')
        .type('form')
        .send({ _csrf: getValidCsrf(), email: 'alice@example.com' });

      await new Promise(r => setTimeout(r, 10));
      const url = (sendPasswordResetEmail as ReturnType<typeof vi.fn>).mock.calls[0][2] as string;
      expect(url).not.toContain('<img');
      expect(url).not.toContain('"');
      expect(url).toMatch(/^http:\/\/localhost\/reset-password\?token=[0-9a-f]{64}$/);
    } finally {
      if (original !== undefined) process.env.BASE_URL = original;
    }
  });

  it('prunes long-expired tokens in the same statement that issues a new one', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ name: 'Alice', email_verified: true }] });

    await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'alice@example.com' });

    const sql = mockQuery.mock.calls[0][0] as string;
    expect(sql).toContain('DELETE FROM password_reset_tokens');
    expect(sql).toContain("expires_at < NOW() - INTERVAL '1 day'");
    expect(sql).toContain('INSERT INTO password_reset_tokens');
    // The raw token is never stored: the parameter is its SHA-256 hex digest.
    expect(mockQuery.mock.calls[0][1][1]).toMatch(/^[0-9a-f]{64}$/);
  });

  it('does not crash on a non-string email field', async () => {
    const res = await request(app)
      .post('/forgot-password')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&email[]=a%40example.com&email[]=b%40example.com');

    expect(res.status).toBe(200);
    expect(res.text).toContain('check your inbox');
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('does NOT include reset link fallback when email is not found (prevents enumeration)', async () => {
    // DB returns no customer
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: csrf, email: 'nobody@example.com' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('check your inbox');
    // Must NOT show a reset link for non-existent users
    expect(res.text).not.toContain('/reset-password?token=');
  });

  it('returns 403 when CSRF token is missing', async () => {
    const res = await request(app)
      .post('/forgot-password')
      .type('form')
      .send({ email: 'x@example.test' });

    expect(res.status).toBe(403);
  });
});

// --- GET /reset-password ---

describe('GET /reset-password', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders an error when no token param is provided', async () => {
    const res = await request(app).get('/reset-password');
    expect(res.status).toBe(400);
    expect(res.text).toContain('Invalid or missing reset token');
  });

  it('renders an expired/invalid error when token is not found in DB', async () => {
    // DB returns no matching valid token
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await request(app).get('/reset-password?token=expiredtoken123');
    expect(res.status).toBe(200);
    expect(res.text).toContain('invalid or has expired');
  });

  it('renders the reset form when token is valid', async () => {
    // DB returns a matching token row
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'tok-1' }] });

    const res = await request(app).get('/reset-password?token=validtoken123');
    expect(res.status).toBe(200);
    // Should show a form, not an error about expiry
    expect(res.text).not.toContain('invalid or has expired');
    expect(res.text).toContain('Reset Password');
  });
});

// --- POST /reset-password ---

describe('POST /reset-password', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders validation error when password is shorter than 8 characters', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send({ _csrf: csrf, token: 'sometoken', password: 'short' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('at least 8 characters');
  });

  it('renders validation error when token field is missing', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send({ _csrf: csrf, token: '', password: 'newpassword1' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('Invalid or missing reset token');
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('rejects a password longer than bcrypt can hash (72 bytes) before touching the DB', async () => {
    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send({ _csrf: getValidCsrf(), token: 'sometoken', password: 'a'.repeat(73) });

    expect(res.status).toBe(200);
    expect(res.text).toContain('Password is too long');
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('does not crash on non-string token or password fields', async () => {
    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&token[]=a&password[x]=longenough1');

    expect(res.status).toBe(200);
    expect(res.text).toContain('Invalid or missing reset token');
  });

  it('renders expired error when token is not found in DB (invalid or used)', async () => {
    // DB returns no matching valid token
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send({ _csrf: csrf, token: 'badtoken', password: 'newpassword1' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('invalid or has expired');
  });

  it('updates password, marks token used, invalidates sessions, and renders success on valid token + password', async () => {
    // Validity pre-check finds the token
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'tok-1' }] });
    // Atomic claim + password update returns the customer
    mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1' }] });
    // Retire any other outstanding reset tokens for the account
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // Retire outstanding verification links (the account is verified now)
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // DELETE user_sessions (session invalidation)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send({ _csrf: csrf, token: 'validtoken', password: 'newpassword1' });

    expect(res.status).toBe(200);
    // One statement both spends the token and sets the password, so the
    // token cannot be used twice by racing requests.
    const claimCall = mockQuery.mock.calls[1];
    expect(claimCall[0]).toContain('UPDATE password_reset_tokens');
    expect(claimCall[0]).toContain('used_at IS NULL');
    expect(claimCall[0]).toContain('UPDATE customers c');
    expect(claimCall[0]).toContain('SET password = $2, email_verified = TRUE');
    // The stored value is a bcrypt hash, never the submitted password.
    expect(claimCall[1][1]).not.toBe('newpassword1');
    expect(String(claimCall[1][1]).startsWith('$2')).toBe(true);
    // Other outstanding reset tokens for the account are retired
    const retireCall = mockQuery.mock.calls[2];
    expect(retireCall[0]).toContain('UPDATE password_reset_tokens SET used_at');
    expect(retireCall[1]).toEqual(['cust-1']);
    // A reset link proves the mailbox, so verification links are retired too
    const retireVerifyCall = mockQuery.mock.calls[3];
    expect(retireVerifyCall[0]).toContain('UPDATE email_verification_tokens SET used_at');
    expect(retireVerifyCall[1]).toEqual(['cust-1']);
    // Sessions invalidated
    const deleteSessionsCall = mockQuery.mock.calls[4];
    expect(deleteSessionsCall[0]).toContain('DELETE FROM user_sessions');
    expect(deleteSessionsCall[1]).toContain('cust-1');
    // Page shows success state
    expect(res.text).not.toContain('invalid or has expired');
    expect(res.text).toContain('Your password is set');
  });

  it('treats a token that another request spent first as invalid (single use under a race)', async () => {
    // Pre-check passes for both racers...
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'tok-1' }] });
    // ...but the atomic claim finds nothing left to claim for the loser.
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send({ _csrf: getValidCsrf(), token: 'validtoken', password: 'newpassword1' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('invalid or has expired');
    // No session purge, no token retirement: nothing was changed.
    expect(mockQuery).toHaveBeenCalledTimes(2);
  });

  it('keeps the token page out of caches, Referer headers and analytics', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'tok-1' }] });
    const original = process.env.PLAUSIBLE_DOMAIN;
    process.env.PLAUSIBLE_DOMAIN = 'analytics.example.test';
    try {
      const res = await request(app).get('/reset-password?token=validtoken123');
      expect(res.headers['cache-control']).toBe('no-store');
      expect(res.headers['referrer-policy']).toBe('no-referrer');
      expect(res.text).not.toContain('plausible.io/js');
    } finally {
      if (original === undefined) delete process.env.PLAUSIBLE_DOMAIN;
      else process.env.PLAUSIBLE_DOMAIN = original;
    }
  });

  it('returns 403 when CSRF token is missing', async () => {
    const res = await request(app)
      .post('/reset-password')
      .type('form')
      .send({ token: 'tok', password: 'newpassword1' });

    expect(res.status).toBe(403);
  });
});

// --- GET /unsubscribe ---

describe('GET /unsubscribe', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 400 with error when no token param is provided', async () => {
    const res = await request(app).get('/unsubscribe');
    expect(res.status).toBe(400);
    expect(res.text).toContain('Invalid unsubscribe link');
  });

  it('renders an error when token does not match any customer', async () => {
    // DB returns empty list of subscribed customers
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await request(app).get('/unsubscribe?token=unknowntoken');
    expect(res.status).toBe(200);
    expect(res.text).toContain('invalid');
  });

  it('opening a valid link changes nothing: it shows a page whose button posts the link back', async () => {
    // Our mock of generateUnsubscribeToken returns `hmac-token-for-${email}`.
    const res = await request(app).get('/unsubscribe?token=hmac-token-for-alice%40example.com&email=alice%40example.com');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Unsubscribe?');
    expect(res.text).toContain('<form action="/unsubscribe" method="POST">');
    expect(res.text).toContain('name="email" value="alice@example.com"');
    expect(res.text).toContain('name="token" value="hmac-token-for-alice@example.com"');
    expect(res.headers['cache-control']).toBe('no-store');
    // Nothing is read or written by opening it.
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('marks customer unsubscribed and renders confirmation when the page\'s button is pressed', async () => {
    const agent = request.agent(app);
    const page = await agent.get('/unsubscribe?token=hmac-token-for-alice%40example.com&email=alice%40example.com');
    const csrf = (page.text.match(/name="_csrf" value="([^"]+)"/) || [])[1];
    // Direct lookup: email + token provided, HMAC verified, single indexed query.
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-1' }],
    });
    // UPDATE customers SET email_unsubscribed
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent
      .post('/unsubscribe')
      .type('form')
      .send({ _csrf: csrf, email: 'alice@example.com', token: 'hmac-token-for-alice@example.com' });
    expect(res.status).toBe(200);

    // The UPDATE query was called
    const updateCall = mockQuery.mock.calls[1];
    expect(updateCall[0]).toContain('UPDATE customers SET email_unsubscribed = TRUE');
    expect(updateCall[1][0]).toBe('cust-1');

    // Page shows confirmation
    expect(res.text).toContain('off the list');
    expect(res.text).not.toContain('invalid');
  });

  it('POST /unsubscribe needs the page\'s CSRF token, and a link whose HMAC does not match changes nothing', async () => {
    const noToken = await request(app)
      .post('/unsubscribe')
      .type('form')
      .send({ email: 'alice@example.com', token: 'hmac-token-for-alice@example.com' });
    expect(noToken.status).toBe(403);

    const forged = await request(app)
      .post('/unsubscribe')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'alice@example.com', token: 'hmac-token-for-bob@example.com' });
    expect(forged.status).toBe(200);
    expect(forged.text).toContain('invalid');
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('returns error when token provided without email', async () => {
    // Legacy links without email param now get invalid link error
    const res = await request(app).get('/unsubscribe?token=hmac-token-for-alice%40example.com');
    expect(res.status).toBe(200);
    expect(res.text).toContain('invalid');
  });
});
