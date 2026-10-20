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

// Mock the email service so sign-up tests don't attempt HTTP requests
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendVerificationEmail: vi.fn().mockResolvedValue(undefined),
  sendPasswordResetEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn().mockReturnValue('mock-token'),
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

// Mock the stripe service: no call to Stripe may leave a unit test
vi.mock('../../src/services/stripe', () => ({
  cancelHarborSubscriptionAtPeriodEnd: vi.fn(),
}));

// Mock the claude service so intake route doesn't call Anthropic
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello! What would you like to build?'),
}));

// Mock the anon-intake service (vi.hoisted so the fn is available inside the hoisted vi.mock factory)
const { mockClaimAnonDraftBySessionId } = vi.hoisted(() => ({
  mockClaimAnonDraftBySessionId: vi.fn().mockResolvedValue({ claimed: false, buildId: null }),
}));
vi.mock('../../src/services/anon-intake', () => ({
  generateAnonSessionId: vi.fn().mockReturnValue('mock-session-id'),
  createAnonDraft: vi.fn().mockResolvedValue({ buildId: 'mock-build-id' }),
  getAnonDraftByBuildId: vi.fn().mockResolvedValue(null),
  claimAnonDraft: vi.fn().mockResolvedValue(false),
  claimAnonDraftBySessionId: mockClaimAnonDraftBySessionId,
}));

// Import the mocked pool so tests can configure return values
import pool from '../../src/db/index';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;

// Helper: get a valid CSRF token
function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

// bcrypt hash for "password123" with 12 rounds - pre-computed to avoid slow bcrypt in tests
// We use bcrypt.hash in the actual sign-up, so for login tests we need a real hash.
// Use a low-round hash for the test fixture.
import bcrypt from 'bcrypt';

async function hashPassword(pw: string): Promise<string> {
  return bcrypt.hash(pw, 1); // 1 round for test speed
}

describe('GET /signup', () => {
  it('returns 200 with HTML', async () => {
    const res = await request(app).get('/signup');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Create Account');
  });
});

describe('POST /signup', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('creates a user and redirects to verify-email-sent', async () => {
    // First query: check existing email -> none found
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // Second query: insert new customer -> return created user
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-1', email: 'test@example.com', name: 'Test User' }],
    });
    // Third query: insert verification token (hashed)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf, email: 'test@example.com', password: 'password123', name: 'Test User' });

    // Should redirect (302) to verify-email-sent (email verification required).
    // The address is not in the URL: it would land in history and proxy logs.
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/verify-email-sent');

    // Verify the stored token is a SHA-256 hash (64 hex chars), not the raw token
    const insertTokenCall = mockQuery.mock.calls[2];
    expect(insertTokenCall[0]).toContain('INSERT INTO email_verification_tokens');
    const storedToken = insertTokenCall[1][1] as string;
    expect(storedToken).toMatch(/^[a-f0-9]{64}$/); // SHA-256 hex output
  });

  it('returns 200 with error when required fields are missing', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf, email: '', password: '', name: '' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('All fields are required');
  });

  it('returns 200 with error when email format is invalid', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf, email: 'not-an-email', password: 'password123', name: 'Alice' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('valid email address');
  });

  it('returns 200 with error when password is too short', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf, email: 'a@example.test', password: 'short', name: 'Alice' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('at least 8 characters');
  });

  it('redirects to verify-email-sent when a verified account already has the email, and changes nothing', async () => {
    // Existing verified account: the response is identical to a new signup,
    // so the form cannot be used to find out which addresses are registered.
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'existing-id', email_verified: true }] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf, email: 'existing@example.com', password: 'password123', name: 'Bob' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/verify-email-sent');
    // The lookup, and nothing after it: no UPDATE, no token, no email.
    expect(mockQuery).toHaveBeenCalledTimes(1);
    expect(String(mockQuery.mock.calls[0][0])).toMatch(/^SELECT id, email_verified FROM customers WHERE email = \$1$/);
  });

  it('returns 403 when CSRF token is missing', async () => {
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ email: 'x@example.test', password: 'password123', name: 'X' });

    expect(res.status).toBe(403);
  });

  it('always redirects to verify-email-sent regardless of returnTo param', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-1', email: 'test@example.com', name: 'Test User' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/signup?returnTo=https://evil.example/steal')
      .type('form')
      .send({ _csrf: csrf, email: 'test@example.com', password: 'password123', name: 'Test User' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/verify-email-sent');
  });

  it('passes UTM params from session to the INSERT query', async () => {
    // First visit GET /signup with UTM params to stash them in session
    const agent = request.agent(app);
    await agent.get('/signup?utm_source=twitter&utm_medium=social&utm_campaign=launch');

    // Mock queries: check existing -> insert customer -> insert token
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-utm', email: 'utm@example.com', name: 'UTM User' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await agent
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf, email: 'utm@example.com', password: 'password123', name: 'UTM User' });

    expect(res.status).toBe(302);

    // Verify the INSERT includes UTM params
    const insertCall = mockQuery.mock.calls.find((c: unknown[]) =>
      typeof c[0] === 'string' && (c[0] as string).includes('INSERT INTO customers')
    );
    expect(insertCall).toBeDefined();
    const sql = insertCall![0] as string;
    expect(sql).toContain('utm_source');
    expect(sql).toContain('utm_medium');
    expect(sql).toContain('utm_campaign');
    const params = insertCall![1] as string[];
    expect(params).toContain('twitter');
    expect(params).toContain('social');
    expect(params).toContain('launch');
  });

  /**
   * Give the agent's session an anonymous draft the way a real visitor gets
   * one: POST /try/start stores anonSessionId (mocked id 'mock-session-id') and
   * anonBuildId on the server-side session.
   */
  async function startAnonDraft(agent: ReturnType<typeof request.agent>): Promise<void> {
    mockQuery.mockResolvedValue({ rows: [], rowCount: 0 });
    const start = await agent.post('/try/start').set('x-csrf-token', getValidCsrf()).send({});
    expect(start.status).toBe(200);
    expect(start.body.buildId).toBe('mock-build-id');
    mockQuery.mockReset();
  }

  function queueSignupQueries(customer: { id: string; email: string; name: string }): void {
    // check existing -> insert customer -> insert token
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [customer] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
  }

  it('claims the anonymous draft on signup when the session carries anonSessionId', async () => {
    mockClaimAnonDraftBySessionId.mockClear();
    mockClaimAnonDraftBySessionId.mockResolvedValueOnce({ claimed: true, buildId: 'anon-build-1' });
    const agent = request.agent(app);
    await startAnonDraft(agent);

    queueSignupQueries({ id: 'cust-anon', email: 'anon@example.com', name: 'Anon User' });
    const res = await agent
      .post('/signup')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'anon@example.com', password: 'password123', name: 'Anon User' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/verify-email-sent');
    // The draft is claimed by the session that created it, for the new account.
    expect(mockClaimAnonDraftBySessionId).toHaveBeenCalledTimes(1);
    expect(mockClaimAnonDraftBySessionId).toHaveBeenCalledWith('mock-session-id', 'cust-anon');

    // The anonymous keys are cleared: a second signup from the same session
    // does not try to claim anything.
    mockClaimAnonDraftBySessionId.mockClear();
    queueSignupQueries({ id: 'cust-two', email: 'second@example.com', name: 'Second' });
    await agent
      .post('/signup')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'second@example.com', password: 'password123', name: 'Second' });
    expect(mockClaimAnonDraftBySessionId).not.toHaveBeenCalled();
  });

  it('does not fail signup when the anonymous draft claim throws', async () => {
    mockClaimAnonDraftBySessionId.mockClear();
    mockClaimAnonDraftBySessionId.mockRejectedValueOnce(new Error('DB connection lost'));
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const agent = request.agent(app);
      await startAnonDraft(agent);

      queueSignupQueries({ id: 'cust-err', email: 'err@example.com', name: 'Error User' });
      const res = await agent
        .post('/signup')
        .type('form')
        .send({ _csrf: getValidCsrf(), email: 'err@example.com', password: 'password123', name: 'Error User' });

      // The claim was attempted and failed, and signup still succeeded.
      expect(mockClaimAnonDraftBySessionId).toHaveBeenCalledTimes(1);
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/verify-email-sent');
      expect(errorSpy.mock.calls.some((call) => String(call[0]).includes('claim on signup failed'))).toBe(true);
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('does not call claimAnonDraftBySessionId when no anonSessionId in session', async () => {
    mockClaimAnonDraftBySessionId.mockClear();

    // Mock queries: check existing -> insert customer -> insert token
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-no-anon', email: 'noanon@example.com', name: 'No Anon' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf, email: 'noanon@example.com', password: 'password123', name: 'No Anon' });

    expect(res.status).toBe(302);
    expect(mockClaimAnonDraftBySessionId).not.toHaveBeenCalled();
  });
});

describe('GET /login', () => {
  it('returns 200 with login form HTML', async () => {
    const res = await request(app).get('/login');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Log In');
  });
});

describe('POST /login', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 200 with error when email format is invalid', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: csrf, email: 'bad-email', password: 'password123' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('valid email address');
  });

  it('sets session and redirects to /dashboard on valid credentials', async () => {
    const hashed = await hashPassword('mypassword');
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-99', email: 'user@example.com', name: 'User', password: hashed, email_verified: true }],
    });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: csrf, email: 'user@example.com', password: 'mypassword' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/dashboard');
  });

  it('returns 200 with unverified prompt when email is not verified', async () => {
    const hashed = await hashPassword('mypassword');
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-99', email: 'user@example.com', name: 'User', password: hashed, email_verified: false }],
    });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: csrf, email: 'user@example.com', password: 'mypassword' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('verify your email');
  });

  it('returns 200 with error on wrong password', async () => {
    const hashed = await hashPassword('correctpassword');
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-99', email: 'user@example.com', name: 'User', password: hashed }],
    });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: csrf, email: 'user@example.com', password: 'wrongpassword' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('Invalid email or password');
  });

  it('returns 200 with error for unknown email', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: csrf, email: 'nobody@example.com', password: 'anything' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('Invalid email or password');
  });

  it('returns 200 with error when fields are missing', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: csrf, email: '', password: '' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('required');
  });

  it('redirects to /dashboard (not an external URL) when returnTo is absolute', async () => {
    const hashed = await hashPassword('mypassword');
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-99', email: 'user@example.com', name: 'User', password: hashed, email_verified: true }],
    });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login?returnTo=https://evil.example')
      .type('form')
      .send({ _csrf: csrf, email: 'user@example.com', password: 'mypassword' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/dashboard');
  });

  it('redirects to /dashboard (not an external URL) when returnTo starts with //', async () => {
    const hashed = await hashPassword('mypassword');
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'cust-99', email: 'user@example.com', name: 'User', password: hashed, email_verified: true }],
    });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/login?returnTo=//evil.example')
      .type('form')
      .send({ _csrf: csrf, email: 'user@example.com', password: 'mypassword' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/dashboard');
  });
});

describe('POST /login hardening', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  const verifiedCustomer = async () => ({
    rows: [{
      id: 'cust-99',
      email: 'user@example.com',
      name: 'User',
      password: await hashPassword('mypassword'),
      email_verified: true,
    }],
  });

  it.each([
    ['backslash authority', '/\\evil.example.test'],
    ['encoded backslash authority', '/%5Cevil.example.test'],
    ['tab before second slash', '/%09/evil.example.test'],
    ['newline in path', '/dashboard%0d%0aSet-Cookie:x=1'],
    ['scheme', 'javascript:alert(1)'],
  ])('ignores an unsafe returnTo (%s)', async (_label, returnTo) => {
    mockQuery.mockResolvedValueOnce(await verifiedCustomer());
    const res = await request(app)
      .post('/login?returnTo=' + returnTo)
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'user@example.com', password: 'mypassword' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/dashboard');
  });

  it('follows a same-site returnTo path', async () => {
    mockQuery.mockResolvedValueOnce(await verifiedCustomer());
    const res = await request(app)
      .post('/login?returnTo=' + encodeURIComponent('/builds/abc?tab=logs'))
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'user@example.com', password: 'mypassword' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/abc?tab=logs');
  });

  it('carries a safe returnTo from the login page into the form action', async () => {
    const res = await request(app).get('/login?returnTo=' + encodeURIComponent('/builds/abc'));
    expect(res.status).toBe(200);
    expect(res.text).toContain('action="/login?returnTo=%2Fbuilds%2Fabc"');

    const unsafe = await request(app).get('/login?returnTo=' + encodeURIComponent('//evil.example.test'));
    expect(unsafe.text).toContain('action="/login"');
    expect(unsafe.text).not.toContain('evil.example.test');
  });

  it('issues a new session id at login (session fixation)', async () => {
    const agent = request.agent(app);
    // Force a pre-login session to exist: GET /signup with a UTM tag stores it.
    const before = await agent.get('/signup?utm_source=newsletter');
    const sidBefore = /cas\.sid=([^;]+)/.exec(String(before.headers['set-cookie']))?.[1];
    expect(sidBefore).toBeTruthy();

    mockQuery.mockResolvedValueOnce(await verifiedCustomer());
    const login = await agent
      .post('/login')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'user@example.com', password: 'mypassword' });
    expect(login.status).toBe(302);
    const sidAfter = /cas\.sid=([^;]+)/.exec(String(login.headers['set-cookie']))?.[1];
    expect(sidAfter).toBeTruthy();
    expect(sidAfter).not.toBe(sidBefore);

    // The id that existed before authentication is not an authenticated session.
    const replay = await request(app)
      .get('/account/delete')
      .set('Cookie', `cas.sid=${sidBefore}`);
    expect(replay.status).toBe(302);
    expect(replay.headers.location).toContain('/login');

    // The new one is.
    const page = await agent.get('/account/delete');
    expect(page.status).toBe(200);
  });

  it('login CSRF: a login form token taken from one browser is refused from another, and works in its own', async () => {
    const tokenOf = (html: string): string => (html.match(/name="_csrf" value="([^"]+)"/) || [])[1] || '';
    // The attacker's browser loads the login page and keeps its token.
    const attacker = request.agent(app);
    const attackerToken = tokenOf((await attacker.get('/login')).text);
    expect(attackerToken.split('.')).toHaveLength(3);

    // The victim's browser, with a session of its own or with none, is made to post it.
    const victim = request.agent(app);
    await victim.get('/');
    for (const client of [victim, request(app)]) {
      mockQuery.mockResolvedValueOnce(await verifiedCustomer());
      const forged = await client
        .post('/login')
        .type('form')
        .send({ _csrf: attackerToken, email: 'user@example.com', password: 'mypassword' });
      expect(forged.status).toBe(403);
    }
    // Refused before the route ran: the account was never looked up, so nobody was logged in.
    expect(mockQuery).not.toHaveBeenCalled();
    mockQuery.mockReset();
    expect((await victim.get('/account/delete')).status).toBe(302);

    // The same token, from the browser whose page it came from, logs in.
    mockQuery.mockReset();
    mockQuery.mockResolvedValueOnce(await verifiedCustomer());
    const own = await attacker
      .post('/login')
      .type('form')
      .send({ _csrf: attackerToken, email: 'user@example.com', password: 'mypassword' });
    expect(own.status).toBe(302);
    expect(own.headers.location).toBe('/dashboard');
  });

  it('a logged-out browser keeps the session its form token is bound to: rendering the page sets the session cookie', async () => {
    const page = await request(app).get('/login');
    expect(String(page.headers['set-cookie'])).toMatch(/^cas\.sid=/);
    // A response that renders nothing leaves no session behind.
    const health = await request(app).get('/robots.txt');
    expect(health.headers['set-cookie']).toBeUndefined();
  });

  it('runs a bcrypt comparison even when the email is unknown (no timing oracle)', async () => {
    const compareSpy = vi.spyOn(bcrypt, 'compare');
    try {
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await request(app)
        .post('/login')
        .type('form')
        .send({ _csrf: getValidCsrf(), email: 'nobody@example.com', password: 'anything' });

      expect(res.status).toBe(200);
      expect(res.text).toContain('Invalid email or password');
      expect(compareSpy).toHaveBeenCalledTimes(1);
      // Compared against a real bcrypt hash, not an empty or undefined value.
      expect(String(compareSpy.mock.calls[0][1]).startsWith('$2')).toBe(true);
    } finally {
      compareSpy.mockRestore();
    }
  });

  it('answers a non-string email or password with the form error, not a crash', async () => {
    const res = await request(app)
      .post('/login')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&email[]=a%40b.example&password[k]=v');

    expect(res.status).toBe(200);
    expect(res.text).toContain('required');
    expect(mockQuery).not.toHaveBeenCalled();
  });
});

describe('POST /signup hardening', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('rejects a password longer than bcrypt can hash (72 bytes)', async () => {
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'new@example.com', password: 'a'.repeat(73), name: 'New' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('Password is too long');
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('counts bytes, not characters, against the bcrypt limit', async () => {
    // 25 four-byte characters: 25 UTF-16 pairs, 100 bytes.
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'new@example.com', password: '\u{1F600}'.repeat(25), name: 'New' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('Password is too long');
  });

  it('answers non-string fields with the form error, not a crash', async () => {
    const res = await request(app)
      .post('/signup')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&email[]=a%40b.example&password=password123&name=N');

    expect(res.status).toBe(200);
    expect(res.text).toContain('All fields are required');
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('hashes the password before the existence check so both outcomes cost the same', async () => {
    const hashSpy = vi.spyOn(bcrypt, 'hash');
    try {
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'existing', email_verified: true }] });
      const res = await request(app)
        .post('/signup')
        .type('form')
        .send({ _csrf: getValidCsrf(), email: 'taken@example.com', password: 'password123', name: 'T' });

      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/verify-email-sent');
      expect(hashSpy).toHaveBeenCalledTimes(1);
    } finally {
      hashSpy.mockRestore();
    }
  });

  it('prunes long-expired verification tokens when issuing a new one', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-1', email: 'p@example.com', name: 'P' }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'p@example.com', password: 'password123', name: 'P' });

    const sql = mockQuery.mock.calls[2][0] as string;
    expect(sql).toContain('DELETE FROM email_verification_tokens');
    expect(sql).toContain('INSERT INTO email_verification_tokens');
  });
});

describe('POST /logout', () => {
  it('clears session and redirects to home', async () => {
    const csrf = generateCsrfToken({} as Request);
    const res = await request(app)
      .post('/logout')
      .type('form')
      .send({ _csrf: csrf });
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/');
  });

  it('expires the session cookie and ends the session server-side', async () => {
    mockQuery.mockReset();
    const agent = await loginAgent('user@example.com');
    expect((await agent.get('/account/delete')).status).toBe(200);

    const res = await agent.post('/logout').type('form').send({ _csrf: getValidCsrf() });
    expect(res.status).toBe(302);
    expect(String(res.headers['set-cookie'])).toMatch(/cas\.sid=;/);

    const after = await agent.get('/account/delete');
    expect(after.status).toBe(302);
    expect(after.headers.location).toContain('/login');
  });
});

// --- Email verification ---

describe('GET /verify-email', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 400 when no token param is provided', async () => {
    const res = await request(app).get('/verify-email');
    expect(res.status).toBe(400);
    expect(res.text).toContain('Invalid or missing');
  });

  it('renders login with error when token is expired or already used', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await request(app).get('/verify-email?token=expired-token');
    expect(res.status).toBe(200);
    expect(res.text).toContain('expired or already been used');
    // A link that is not waiting stops at the lookup: nothing is written.
    expect(mockQuery).toHaveBeenCalledTimes(1);
    expect(String(mockQuery.mock.calls[0][0])).toMatch(/^SELECT c\.email/);
  });

  /**
   * Sign up through the real route so the agent's session holds the claim for
   * `email`, then queue what GET /verify-email will ask the database.
   */
  async function signedUpAgent(email: string, name: string): Promise<ReturnType<typeof request.agent>> {
    const agent = request.agent(app);
    mockQuery.mockResolvedValueOnce({ rows: [] }); // lookup: free address
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-1', email, name }] }); // insert customer
    mockQuery.mockResolvedValueOnce({ rows: [] }); // insert token
    const signup = await agent
      .post('/signup')
      .type('form')
      .send({ _csrf: getValidCsrf(), email, password: 'password123', name });
    expect(signup.status).toBe(302);
    mockQuery.mockReset();
    return agent;
  }

  /**
   * Open the emailed link the way a browser does: GET shows the confirmation
   * page (one read, nothing spent), and its button posts the token back.
   */
  async function openLink(agent: ReturnType<typeof request.agent>, token: string): Promise<request.Response> {
    const page = await agent.get(`/verify-email?token=${token}`);
    expect(page.status).toBe(200);
    expect(page.text).toContain('action="/verify-email"');
    expect(page.text).toContain(`name="token" value="${token}"`);
    const csrf = (page.text.match(/name="_csrf" value="([^"]+)"/) || [])[1];
    return agent.post('/verify-email').type('form').send({ _csrf: csrf, token });
  }

  it('opening the link spends nothing: one read, then a page whose button posts the token back', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'user@example.test' }] });
    const res = await request(app).get('/verify-email?token=valid-token-abc');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Confirm your email.');
    expect(res.text).toMatch(/<form action="\/verify-email" method="POST">/);
    expect(res.headers['cache-control']).toBe('no-store');
    expect(res.headers['referrer-policy']).toBe('no-referrer');
    expect(mockQuery).toHaveBeenCalledTimes(1);
    expect(String(mockQuery.mock.calls[0][0])).toMatch(/^SELECT c\.email/);
  });

  it('POST /verify-email needs the form\'s CSRF token', async () => {
    const res = await request(app).post('/verify-email').type('form').send({ token: 'valid-token-abc' });
    expect(res.status).toBe(403);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('verifies customer and redirects to /intake on valid token', async () => {
    const agent = await signedUpAgent('user@example.test', 'Test');
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'user@example.test' }] }); // the page's look: still waiting
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'user@example.test' }] }); // which account is waiting
    // One atomic statement claims the token, verifies the customer and settles the credentials.
    mockQuery.mockResolvedValueOnce({
      rows: [{ customer_id: 'cust-1', email: 'user@example.test', name: 'Test', own_claim: true }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // retire other links
    mockQuery.mockResolvedValueOnce({ rows: [] }); // expire paid trial cookies promoted into the account

    const res = await openLink(agent, 'valid-token-abc');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/intake');

    // A single statement does the spending and the verifying, so the token
    // cannot be spent twice by racing requests and there is no pool-level
    // BEGIN/COMMIT (which would not be a transaction: each pool.query may run
    // on a different connection).
    expect(mockQuery).toHaveBeenCalledTimes(5);
    const [sql, params] = mockQuery.mock.calls[2];
    expect(sql).toContain('UPDATE email_verification_tokens');
    expect(sql).toContain('used_at IS NULL');
    expect(sql).toContain('expires_at > NOW()');
    expect(sql).toContain('SET email_verified = TRUE');
    // It never applies to an account that is already verified.
    expect(sql).toContain('AND c.email_verified IS NOT TRUE');
    // This browser's own claim also stamps the terms on a row that has none.
    expect(sql).toMatch(/terms_accepted_at = CASE WHEN \$2::text IS NOT NULL AND c\.email = \$2::text\s+THEN COALESCE\(c\.terms_accepted_at, NOW\(\)\)\s+ELSE c\.terms_accepted_at END/);
    // Looked up by SHA-256 of the token, never the raw value.
    expect(params[0]).toMatch(/^[0-9a-f]{64}$/);
    expect(params[0]).not.toBe('valid-token-abc');
    // A verified mailbox expires the paid trial cookies promoted into the account.
    const [expirySql, expiryParams] = mockQuery.mock.calls[4];
    expect(expirySql).toContain('UPDATE trial_keepers SET expires_at = NOW() WHERE promoted_customer_id = $1');
    expect(expiryParams).toEqual(['cust-1']);
  });

  it('logs the customer in on a fresh session after verification', async () => {
    const agent = await signedUpAgent('user@example.test', 'Test');
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'user@example.test' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'user@example.test' }] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ customer_id: 'cust-1', email: 'user@example.test', name: 'Test', own_claim: true }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await openLink(agent, 'valid-token-abc');
    expect(res.status).toBe(302);
    expect(String(res.headers['set-cookie'])).toContain('cas.sid=');

    // The session is authenticated: a protected page no longer redirects to /login.
    const page = await agent.get('/account/delete');
    expect(page.status).toBe(200);
  });

  it('does not crash on a non-string token', async () => {
    const res = await request(app).get('/verify-email?token[]=a&token[]=b');
    expect(res.status).toBe(400);
    expect(res.text).toContain('Invalid or missing');
    expect(mockQuery).not.toHaveBeenCalled();
  });
});

describe('POST /resend-verification', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('redirects to verify-email-sent for a valid unverified email, and mails the link after the response', async () => {
    const { sendVerificationEmail } = await import('../../src/services/email');
    (sendVerificationEmail as ReturnType<typeof vi.fn>).mockClear();
    // The one statement inserted a token (an unverified account, no link in the last hour).
    mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1' }] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/resend-verification')
      .type('form')
      .send({ _csrf: csrf, email: 'test@example.com' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/verify-email-sent');
    expect(mockQuery).toHaveBeenCalledTimes(1);
    const [sql, params] = mockQuery.mock.calls[0];
    expect(sql).toContain('INSERT INTO email_verification_tokens');
    expect(params[0]).toBe('test@example.com');
    expect(params[1]).toMatch(/^[0-9a-f]{64}$/);
    await vi.waitFor(() => expect(sendVerificationEmail).toHaveBeenCalledTimes(1));
    // The verification mail is not given a name to print.
    expect((sendVerificationEmail as ReturnType<typeof vi.fn>).mock.calls[0]).toEqual([
      'test@example.com',
      expect.stringMatching(/\/verify-email\?token=[0-9a-f]{64}$/),
    ]);
  });

  it('the one statement inserts only for an unverified account that was sent no link in the last hour', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });
    await request(app).post('/resend-verification').type('form').send({ _csrf: getValidCsrf(), email: 'test@example.com' });
    const sql = String(mockQuery.mock.calls[0][0]);
    expect(sql).toContain('c.email_verified IS NOT TRUE');
    // A link lives 24 hours, so one issued in the last hour expires more than 23 hours from now.
    expect(sql).toMatch(/NOT EXISTS \(\s*SELECT 1 FROM email_verification_tokens t\s+WHERE t\.customer_id = c\.id\s+AND t\.expires_at > NOW\(\) \+ INTERVAL '23 hours'\)/);
    expect(sql).toMatch(/INSERT INTO email_verification_tokens \(customer_id, token\)\s+SELECT id, \$2 FROM target/);
  });

  it('redirects to verify-email-sent even for already verified email (no leak)', async () => {
    const { sendVerificationEmail } = await import('../../src/services/email');
    (sendVerificationEmail as ReturnType<typeof vi.fn>).mockClear();
    // The statement inserted nothing: the account is verified.
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/resend-verification')
      .type('form')
      .send({ _csrf: csrf, email: 'verified@example.com' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/verify-email-sent');
    expect(mockQuery).toHaveBeenCalledTimes(1);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(sendVerificationEmail).not.toHaveBeenCalled();
  });

  it('runs the same statements for an unverified, a verified and an unknown address', async () => {
    const sequences: unknown[][] = [];
    for (const answer of [[{ customer_id: 'cust-1' }], [], []]) {
      mockQuery.mockReset();
      mockQuery.mockResolvedValueOnce({ rows: answer });
      await request(app).post('/resend-verification').type('form').send({ _csrf: getValidCsrf(), email: 'someone@example.com' });
      sequences.push(mockQuery.mock.calls.map(([sql, params]) => [sql, (params as unknown[]).length, (params as unknown[])[0]]));
    }
    expect(sequences[0]).toHaveLength(1);
    expect(sequences[1]).toEqual(sequences[0]);
    expect(sequences[2]).toEqual(sequences[0]);
  });

  it('redirects to verify-email-sent for unknown email (no leak)', async () => {
    // No customer found
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/resend-verification')
      .type('form')
      .send({ _csrf: csrf, email: 'unknown@example.com' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/verify-email-sent');
  });
});

// Helper: create an authenticated supertest agent
async function loginAgent(email: string): Promise<request.Agent> {
  const agent = request.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'cust-1', email, name: 'Test User', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email, password: 'password123' });
  return agent;
}

describe('GET /account/delete', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app).get('/account/delete');
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/login');
  });

  it('renders the account deletion confirmation page', async () => {
    const agent = await loginAgent('user@example.com');
    const res = await agent.get('/account/delete');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Delete Account');
  });
});

describe('POST /account/delete', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app)
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/login');
  });

  it('returns 403 without CSRF token', async () => {
    const agent = await loginAgent('user@example.com');
    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ password: 'password123' });
    expect(res.status).toBe(403);
  });

  it('re-renders with error when password is empty', async () => {
    const agent = await loginAgent('user@example.com');
    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: '' });
    expect(res.status).toBe(200);
    expect(res.text).toContain('Please enter your password to confirm');
  });

  it('re-renders with error when password is incorrect', async () => {
    const agent = await loginAgent('user@example.com');
    const wrongHash = await bcrypt.hash('different_password', 1);
    mockQuery.mockResolvedValueOnce({
      rows: [{ password: wrongHash }],
    });
    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(200);
    expect(res.text).toContain('Incorrect password');
  });

  it('re-renders with error when account not found', async () => {
    const agent = await loginAgent('user@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(200);
    expect(res.text).toContain('Account not found');
  });

  // The deletion runs in this order: password check, the account's open
  // subscriptions (both on the pool), the Stripe cancellations with no
  // transaction open, then one transaction on a checked-out client.
  const OPEN_SUBSCRIPTIONS = /FROM harbor_subscriptions\s+WHERE keeper_customer_id = \$1 AND status <> 'canceled'/;

  /** A transaction client that answers every statement with no rows, except where `respond` says otherwise. */
  function transactionClient(respond: (sql: string) => { rows: unknown[] } | undefined = () => undefined) {
    return {
      query: vi.fn((sql: string) => Promise.resolve(respond(sql) ?? { rows: [] })),
      release: vi.fn(),
    };
  }

  it('deletes account with cascading deletes and redirects on success', async () => {
    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // no open subscriptions

    const mockClient = transactionClient();
    (pool as any).connect = vi.fn().mockResolvedValue(mockClient);

    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/?deleted=1');

    const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
    expect(queries[0]).toBe('BEGIN');
    expect(queries[queries.length - 1]).toBe('COMMIT');
    expect(queries.some((q: string) => q.includes('DELETE FROM intake_messages'))).toBe(true);
    expect(queries.some((q: string) => q.includes('DELETE FROM feature_messages'))).toBe(true);
    expect(queries.some((q: string) => q.includes('DELETE FROM feature_requests'))).toBe(true);
    expect(queries.some((q: string) => q.includes('DELETE FROM beta_feedback'))).toBe(true);
    expect(queries.some((q: string) => q.includes('DELETE FROM email_schedule'))).toBe(true);
    expect(queries.some((q: string) => q.includes('DELETE FROM customers'))).toBe(true);
    expect(queries.some((q: string) => q.includes('UPDATE builds SET customer_id = NULL'))).toBe(true);
    expect(mockClient.release).toHaveBeenCalled();
  });

  it('locks the account\'s builds and then the account before it reads the subscriptions again', async () => {
    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const mockClient = transactionClient();
    (pool as any).connect = vi.fn().mockResolvedValue(mockClient);

    await agent.post('/account/delete').type('form').send({ _csrf: getValidCsrf(), password: 'password123' });

    const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
    // The same order the payment webhook takes its locks in: build, trial, then customer.
    expect(queries[1]).toBe('SELECT id FROM builds WHERE customer_id = $1 FOR UPDATE');
    expect(queries[2]).toMatch(/^SELECT t\.trial_id FROM trial_keepers t JOIN customers c ON c\.id = \$1[\s\S]+FOR UPDATE OF t$/);
    expect(queries[3]).toBe('SELECT id FROM customers WHERE id = $1 FOR UPDATE');
    expect(queries[4]).toMatch(OPEN_SUBSCRIPTIONS);
  });

  it('deletes the account\'s Haven trials while its builds still name it, then clears the address from any trial left', async () => {
    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const mockClient = transactionClient();
    (pool as any).connect = vi.fn().mockResolvedValue(mockClient);

    await agent.post('/account/delete').type('form').send({ _csrf: getValidCsrf(), password: 'password123' });

    const calls = mockClient.query.mock.calls as unknown as Array<[string, unknown[]?]>;
    const queries = calls.map((c) => c[0]);
    const deleteTrials = queries.findIndex((q) => q.startsWith('DELETE FROM trial_keepers t USING customers c WHERE c.id = $1'));
    const clearAddress = queries.findIndex((q) => /UPDATE trial_keepers t SET email_captured = NULL/.test(q));
    const unlinkBuilds = queries.findIndex((q) => q.includes('UPDATE builds SET customer_id = NULL'));
    const deleteCustomer = queries.findIndex((q) => q.includes('DELETE FROM customers'));
    expect(deleteTrials).toBeGreaterThan(-1);
    // A trial is the account's by a build of the account stamped on it, or by
    // the account's address, compared without regard to case, unless another
    // account's build is stamped on it.
    expect(queries[deleteTrials]).toMatch(/t\.handoff_build_id IN \(SELECT id FROM builds WHERE customer_id = \$1\)/);
    expect(queries[deleteTrials]).toMatch(/OR \(LOWER\(BTRIM\(t\.email_captured\)\) = LOWER\(c\.email\)\s+AND NOT EXISTS/);
    expect(queries[deleteTrials]).toMatch(/b\.customer_id IS NOT NULL\s+AND b\.customer_id <> \$1/);
    expect(calls[deleteTrials][1]).toEqual(['cust-1']);
    expect(calls[clearAddress][1]).toEqual(['cust-1']);
    // The lock taken at the top covers the same rows.
    expect(queries[2].replace(/^SELECT t\.trial_id FROM trial_keepers t JOIN customers c ON /, '').replace(/ FOR UPDATE OF t$/, '')).toBe(
      queries[deleteTrials].replace('DELETE FROM trial_keepers t USING customers c WHERE ', '')
    );
    expect(deleteTrials).toBeLessThan(clearAddress);
    expect(clearAddress).toBeLessThan(unlinkBuilds);
    expect(unlinkBuilds).toBeLessThan(deleteCustomer);
    expect(queries[clearAddress]).toMatch(/WHERE c\.id = \$1 AND LOWER\(BTRIM\(t\.email_captured\)\) = LOWER\(c\.email\)/);
  });

  it('closes out builds still waiting in the queue, which no worker could claim once the customer link is gone', async () => {
    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const mockClient = transactionClient();
    (pool as any).connect = vi.fn().mockResolvedValue(mockClient);

    await agent.post('/account/delete').type('form').send({ _csrf: getValidCsrf(), password: 'password123' });

    const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
    const failQueued = queries.findIndex((q: string) => /UPDATE builds\s+SET status = 'failed'/.test(q));
    const anonymize = queries.findIndex((q: string) => q.includes('UPDATE builds SET customer_id = NULL'));
    expect(failQueued).toBeGreaterThan(-1);
    expect(queries[failQueued]).toMatch(/WHERE customer_id = \$1 AND status = 'queued'/);
    // While the builds can still be found by their customer.
    expect(failQueued).toBeLessThan(anonymize);
  });

  it('cancels a live Stripe subscription before any transaction is open, leaves a tombstone, and purges other sessions', async () => {
    const order: string[] = [];
    const stripe = await import('../../src/services/stripe');
    const cancel = vi.fn().mockImplementation(async () => {
      order.push('stripe');
      return {};
    });
    (stripe as unknown as Record<string, unknown>).cancelHarborSubscriptionAtPeriodEnd = cancel;

    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    const realSubscription = ['sub', 'live123'].join('_');
    const bypassSubscription = ['sub', 'bypass', 'abc'].join('_');
    const subscriptions = { rows: [{ stripe_subscription_id: realSubscription }, { stripe_subscription_id: bypassSubscription }] };
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce(subscriptions); // open subscriptions, read on the pool

    const mockClient = transactionClient((sql) => (OPEN_SUBSCRIPTIONS.test(sql) ? subscriptions : undefined));
    (pool as any).connect = vi.fn().mockImplementation(async () => {
      order.push('connect');
      return mockClient;
    });

    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/?deleted=1');

    // Only the subscription that exists at Stripe is cancelled.
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledWith(realSubscription);
    // Stripe was called first, with no client checked out and so no transaction open.
    expect(order).toEqual(['stripe', 'connect']);
    expect(String(mockQuery.mock.calls[mockQuery.mock.calls.length - 1][0])).toMatch(OPEN_SUBSCRIPTIONS);

    const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
    expect(queries.some((q: string) => q.includes('DELETE FROM user_sessions'))).toBe(true);
    // A tombstone per subscription is written before the cascade removes the rows.
    const tombstoneIdx = queries.findIndex((q: string) => q.includes('INSERT INTO harbor_subscription_tombstones'));
    const deleteIdx = queries.findIndex((q: string) => q.includes('DELETE FROM customers'));
    expect(tombstoneIdx).toBeGreaterThan(-1);
    expect(tombstoneIdx).toBeLessThan(deleteIdx);
    expect(queries[tombstoneIdx]).toMatch(/SELECT stripe_subscription_id FROM harbor_subscriptions WHERE keeper_customer_id = \$1/);
  });

  it('keeps the account when the Stripe cancellation fails: no transaction is ever opened', async () => {
    const stripe = await import('../../src/services/stripe');
    (stripe as unknown as Record<string, unknown>).cancelHarborSubscriptionAtPeriodEnd =
      vi.fn().mockRejectedValue(new Error('stripe unreachable'));

    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ stripe_subscription_id: ['sub', 'live123'].join('_') }] });

    const connect = vi.fn();
    (pool as any).connect = connect;

    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(200);
    expect(res.text).toContain('Something went wrong');

    expect(connect).not.toHaveBeenCalled();
    expect(mockQuery.mock.calls.some(([sql]) => /DELETE FROM customers/.test(String(sql)))).toBe(false);
  });

  it('stops, and deletes nothing, when a subscription was recorded after Stripe was asked', async () => {
    const stripe = await import('../../src/services/stripe');
    const cancel = vi.fn().mockResolvedValue({});
    (stripe as unknown as Record<string, unknown>).cancelHarborSubscriptionAtPeriodEnd = cancel;

    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // none when Stripe was (not) called

    // Under the account lock there is one: a checkout landed in between.
    const late = { rows: [{ stripe_subscription_id: ['sub', 'late456'].join('_') }] };
    const mockClient = transactionClient((sql) => (OPEN_SUBSCRIPTIONS.test(sql) ? late : undefined));
    (pool as any).connect = vi.fn().mockResolvedValue(mockClient);

    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(200);
    expect(res.text).toContain('Something went wrong');
    expect(cancel).not.toHaveBeenCalled();

    const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
    expect(queries).toContain('ROLLBACK');
    expect(queries).not.toContain('COMMIT');
    expect(queries.some((q: string) => q.includes('DELETE FROM customers'))).toBe(false);
    expect(mockClient.release).toHaveBeenCalled();
  });

  it('re-renders with error when transaction fails', async () => {
    const agent = await loginAgent('user@example.com');
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({ rows: [{ password: hashed }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const mockClient = {
      query: vi.fn()
        .mockResolvedValueOnce({ rows: [] }) // BEGIN
        .mockRejectedValueOnce(new Error('FK constraint')), // the first statement fails
      release: vi.fn(),
    };
    (pool as any).connect = vi.fn().mockResolvedValue(mockClient);

    const res = await agent
      .post('/account/delete')
      .type('form')
      .send({ _csrf: getValidCsrf(), password: 'password123' });
    expect(res.status).toBe(200);
    expect(res.text).toContain('Something went wrong');
    expect(mockClient.release).toHaveBeenCalled();
  });
});
