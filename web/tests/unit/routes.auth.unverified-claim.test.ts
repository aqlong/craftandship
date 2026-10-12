// Who ends up with the account when an address was claimed before its owner
// proved the mailbox?
//
// These tests drive the real signup, login, verification and reset routes
// through several separate browsers (supertest agents, each with its own
// session) against an in-memory stand-in for the customers and token tables.
// The stand-in implements what each statement is documented to do, which
// makes whole sequences testable (attacker claims, owner claims, somebody
// opens a link). That it matches what Postgres does with the real statements
// is checked separately, by money-path.realdb.test.ts.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import request from 'supertest';
import bcrypt from 'bcrypt';
import crypto from 'crypto';
import type { Request } from 'express';

vi.mock('../../src/db/index', () => ({
  default: { query: vi.fn(), connect: vi.fn(), on: vi.fn() },
}));

// Real bcrypt, at a low cost factor: these tests sign up and log in dozens of
// times, and the routes' production cost of 12 would make that take seconds.
vi.mock('bcrypt', async (importOriginal) => {
  const actual = await importOriginal<typeof import('bcrypt')>();
  const real = (actual as unknown as { default: typeof import('bcrypt') }).default ?? actual;
  const fast = { ...real, hash: (data: string | Buffer, _rounds: number | string) => real.hash(data, 4) };
  return { ...fast, default: fast };
});

vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendVerificationEmail: vi.fn().mockResolvedValue(undefined),
  sendPasswordResetEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn().mockReturnValue('mock-token'),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));
vi.mock('../../src/services/stripe', () => ({
  cancelHarborSubscriptionAtPeriodEnd: vi.fn(),
}));

import app from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import { sendVerificationEmail, sendPasswordResetEmail, sendWelcomeEmail } from '../../src/services/email';

const mockQuery = pool.query as unknown as ReturnType<typeof vi.fn>;
const mockSendVerification = sendVerificationEmail as unknown as ReturnType<typeof vi.fn>;
const mockSendReset = sendPasswordResetEmail as unknown as ReturnType<typeof vi.fn>;
const mockSendWelcome = sendWelcomeEmail as unknown as ReturnType<typeof vi.fn>;

interface Customer {
  id: string;
  email: string;
  password: string;
  name: string;
  email_verified: boolean;
}
interface Token {
  customer_id: string;
  token: string;
  used: boolean;
}

let customers: Customer[];
let verificationTokens: Token[];
let resetTokens: Token[];
let statements: string[];

const sha256 = (value: string): string => crypto.createHash('sha256').update(value).digest('hex');

function fakeDatabase(sql: string, params: unknown[]): { rows: unknown[]; rowCount?: number } {
  statements.push(sql);
  const p = params as string[];

  if (/^SELECT id, email_verified FROM customers WHERE email = \$1/.test(sql)) {
    return { rows: customers.filter((c) => c.email === p[0]).map((c) => ({ id: c.id, email_verified: c.email_verified })) };
  }
  if (/UPDATE customers\s+SET password = \$2, name = \$3, terms_accepted_at = NOW\(\)\s+WHERE id = \$1 AND email_verified IS NOT TRUE/.test(sql)) {
    const row = customers.find((c) => c.id === p[0] && !c.email_verified);
    if (!row) return { rows: [] };
    row.password = p[1];
    row.name = p[2];
    return { rows: [{ id: row.id, email: row.email, name: row.name }] };
  }
  if (/INSERT INTO customers \(email, password, name, terms_accepted_at/.test(sql)) {
    if (customers.some((c) => c.email === p[0])) return { rows: [] };
    const row: Customer = { id: `cust-${customers.length + 1}`, email: p[0], password: p[1], name: p[2], email_verified: false };
    customers.push(row);
    return { rows: [{ id: row.id, email: row.email, name: row.name }] };
  }
  if (/^UPDATE email_verification_tokens SET used_at = NOW\(\) WHERE customer_id = \$1 AND used_at IS NULL/.test(sql)) {
    for (const token of verificationTokens) if (token.customer_id === p[0]) token.used = true;
    return { rows: [] };
  }
  if (/INSERT INTO email_verification_tokens \(customer_id, token\)/.test(sql)) {
    verificationTokens.push({ customer_id: p[0], token: p[1], used: false });
    return { rows: [] };
  }
  if (/^SELECT c\.email\s+FROM email_verification_tokens t\s+JOIN customers c/.test(sql)) {
    const token = verificationTokens.find((t) => t.token === p[0] && !t.used);
    const row = token && customers.find((c) => c.id === token.customer_id && !c.email_verified);
    return { rows: row ? [{ email: row.email }] : [] };
  }
  if (/UPDATE customers c\s+SET email_verified = TRUE,\s+password = CASE/.test(sql)) {
    const token = verificationTokens.find((t) => t.token === p[0] && !t.used);
    if (!token) return { rows: [] };
    token.used = true;
    const row = customers.find((c) => c.id === token.customer_id && !c.email_verified);
    if (!row) return { rows: [] };
    const ownClaim = p[1] !== null && p[1] === row.email;
    row.email_verified = true;
    row.password = ownClaim ? p[2] : p[4];
    row.name = ownClaim ? p[3] : row.email.split('@')[0];
    return { rows: [{ customer_id: row.id, email: row.email, name: row.name, own_claim: ownClaim }] };
  }
  if (/INSERT INTO password_reset_tokens \(customer_id, token\)/.test(sql)) {
    resetTokens.push({ customer_id: p[0], token: p[1], used: false });
    return { rows: [] };
  }
  if (/^SELECT id FROM password_reset_tokens\s+WHERE token = \$1/.test(sql)) {
    return { rows: resetTokens.filter((t) => t.token === p[0] && !t.used).map(() => ({ id: 'reset-token' })) };
  }
  if (/UPDATE customers c\s+SET password = \$2, email_verified = TRUE/.test(sql)) {
    const token = resetTokens.find((t) => t.token === p[0] && !t.used);
    if (!token) return { rows: [] };
    token.used = true;
    const row = customers.find((c) => c.id === token.customer_id);
    if (!row) return { rows: [] };
    row.password = p[1];
    row.email_verified = true;
    return { rows: [{ customer_id: row.id }] };
  }
  if (/^SELECT id, email, name, password, email_verified FROM customers WHERE email = \$1/.test(sql)) {
    return { rows: customers.filter((c) => c.email === p[0]).map((c) => ({ ...c })) };
  }
  if (/^SELECT id, name, email_verified FROM customers WHERE email = \$1/.test(sql)) {
    return { rows: customers.filter((c) => c.email === p[0]).map((c) => ({ id: c.id, name: c.name, email_verified: c.email_verified })) };
  }
  if (/^SELECT id, name FROM customers WHERE email = \$1/.test(sql)) {
    return { rows: customers.filter((c) => c.email === p[0]).map((c) => ({ id: c.id, name: c.name })) };
  }
  return { rows: [], rowCount: 0 };
}

const csrf = (): string => generateCsrfToken({} as Request);
type Browser = ReturnType<typeof request.agent>;
const browser = (): Browser => request.agent(app);

async function signUp(agent: Browser, email: string, password: string, name: string) {
  return agent.post('/signup').type('form').send({ _csrf: csrf(), email, password, name });
}
async function logIn(agent: Browser, email: string, password: string) {
  return agent.post('/login').type('form').send({ _csrf: csrf(), email, password });
}
/** The most recent verification link "sent" to the mailbox, as a path. */
function latestVerificationLink(): string {
  const calls = mockSendVerification.mock.calls;
  const url = String(calls[calls.length - 1][2]);
  return url.slice(url.indexOf('/verify-email'));
}
function latestResetLink(): string {
  const calls = mockSendReset.mock.calls;
  const url = String(calls[calls.length - 1][2]);
  return url.slice(url.indexOf('/reset-password'));
}
const account = (email: string): Customer => customers.find((c) => c.email === email) as Customer;

// Passwords are generated per run; nothing here is a real credential.
const ATTACKER_PASSWORD = `attacker-${crypto.randomBytes(6).toString('hex')}`;
const OWNER_PASSWORD = `owner-${crypto.randomBytes(6).toString('hex')}`;
const VICTIM = 'victim@example.test';

beforeEach(() => {
  customers = [];
  verificationTokens = [];
  resetTokens = [];
  statements = [];
  mockQuery.mockReset().mockImplementation((sql: unknown, params?: unknown) =>
    Promise.resolve(fakeDatabase(String(sql).trim(), Array.isArray(params) ? params : []))
  );
  mockSendVerification.mockClear();
  mockSendReset.mockClear();
  mockSendWelcome.mockClear();
});

describe('an address claimed by someone else before its owner signs up', () => {
  it('the owner signs up later, opens their link, and has their own password and name; the first claimant has nothing', async () => {
    const attacker = browser();
    const owner = browser();

    await signUp(attacker, VICTIM, ATTACKER_PASSWORD, 'Mallory');
    const attackerLink = latestVerificationLink();
    const attackerHash = account(VICTIM).password;

    const ownerSignup = await signUp(owner, VICTIM, OWNER_PASSWORD, 'Vera Owner');
    expect(ownerSignup.status).toBe(302);
    // The owner's signup took over the unverified row straight away...
    expect(account(VICTIM).password).not.toBe(attackerHash);
    expect(await bcrypt.compare(OWNER_PASSWORD, account(VICTIM).password)).toBe(true);
    expect(account(VICTIM).name).toBe('Vera Owner');
    expect(account(VICTIM).email_verified).toBe(false);
    // ...and sent the owner a link of their own.
    const ownerLink = latestVerificationLink();
    expect(ownerLink).not.toBe(attackerLink);

    const verified = await owner.get(ownerLink);
    expect(verified.status).toBe(302);
    expect(verified.headers.location).toBe('/intake');
    expect(account(VICTIM).email_verified).toBe(true);
    expect(account(VICTIM).name).toBe('Vera Owner');

    const attackerLogin = await logIn(browser(), VICTIM, ATTACKER_PASSWORD);
    expect(attackerLogin.status).toBe(200);
    expect(attackerLogin.text).toContain('Invalid email or password');
    const ownerLogin = await logIn(browser(), VICTIM, OWNER_PASSWORD);
    expect(ownerLogin.status).toBe(302);
    expect(ownerLogin.headers.location).toBe('/dashboard');
  });

  it('the link issued for the first claim stops working once someone else claims the address', async () => {
    const attacker = browser();
    await signUp(attacker, VICTIM, ATTACKER_PASSWORD, 'Mallory');
    const attackerLink = latestVerificationLink();
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');

    // Even opened in the attacker's own browser, where their claim is stored.
    const res = await attacker.get(attackerLink);
    expect(res.status).toBe(200);
    expect(res.text).toContain('expired or already been used');
    expect(account(VICTIM).email_verified).toBe(false);
  });

  it('the first claimant claims AGAIN after the owner; the owner opens the newest link and still gets their own password', async () => {
    const attacker = browser();
    const owner = browser();
    await signUp(owner, VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await signUp(attacker, VICTIM, ATTACKER_PASSWORD, 'Mallory');
    // The row now carries the attacker's claim, and the only live link is the
    // one the attacker's signup sent to the owner's mailbox.
    expect(await bcrypt.compare(ATTACKER_PASSWORD, account(VICTIM).password)).toBe(true);
    const newestLink = latestVerificationLink();

    const verified = await owner.get(newestLink);
    expect(verified.status).toBe(302);
    expect(verified.headers.location).toBe('/intake');

    expect(account(VICTIM).email_verified).toBe(true);
    expect(account(VICTIM).name).toBe('Vera Owner');
    expect(await bcrypt.compare(OWNER_PASSWORD, account(VICTIM).password)).toBe(true);
    expect((await logIn(browser(), VICTIM, ATTACKER_PASSWORD)).status).toBe(200);
    expect((await logIn(browser(), VICTIM, OWNER_PASSWORD)).status).toBe(302);
  });

  it('the owner never signed up and opens a link the claimant triggered: the claimant\'s password and name do not survive, and the owner chooses a password', async () => {
    await signUp(browser(), VICTIM, ATTACKER_PASSWORD, 'Mallory');
    const attackerHash = account(VICTIM).password;
    const link = latestVerificationLink();

    const owner = browser();
    const verified = await owner.get(link);
    expect(verified.status).toBe(302);
    expect(verified.headers.location).toMatch(/^\/reset-password\?token=[0-9a-f]{64}&verified=1$/);

    expect(account(VICTIM).email_verified).toBe(true);
    expect(account(VICTIM).password).not.toBe(attackerHash);
    expect(account(VICTIM).password).toMatch(/^\$2[aby]\$/);
    expect(await bcrypt.compare(ATTACKER_PASSWORD, account(VICTIM).password)).toBe(false);
    expect(account(VICTIM).name).toBe('victim');
    // Nobody is logged in by this: the account has no password its owner knows yet.
    expect((await owner.get('/dashboard')).status).toBe(302);
    expect((await logIn(browser(), VICTIM, ATTACKER_PASSWORD)).status).toBe(200);

    // The page the owner lands on says what happened and takes a password.
    const page = await owner.get(String(verified.headers.location));
    expect(page.status).toBe(200);
    expect(page.text).toContain('Your email address is verified. Choose a password');
    const token = new URL(String(verified.headers.location), 'http://localhost').searchParams.get('token') as string;
    const set = await owner.post('/reset-password').type('form').send({ _csrf: csrf(), token, password: OWNER_PASSWORD });
    expect(set.text).toContain('Your password is set');
    expect((await logIn(browser(), VICTIM, OWNER_PASSWORD)).status).toBe(302);
  });
});

describe('a verified account is its owner\'s', () => {
  async function verifiedOwner(): Promise<void> {
    const owner = browser();
    await signUp(owner, VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await owner.get(latestVerificationLink());
    expect(account(VICTIM).email_verified).toBe(true);
    mockSendVerification.mockClear();
    statements = [];
  }

  it('a signup against it changes nothing, writes nothing and sends nothing', async () => {
    await verifiedOwner();
    const before = { ...account(VICTIM) };

    const res = await signUp(browser(), VICTIM, ATTACKER_PASSWORD, 'Mallory');

    expect(res.status).toBe(302);
    expect(account(VICTIM)).toEqual(before);
    expect(statements.filter((sql) => /^(UPDATE|INSERT|WITH)/.test(sql))).toHaveLength(0);
    expect(mockSendVerification).not.toHaveBeenCalled();
    expect((await logIn(browser(), VICTIM, ATTACKER_PASSWORD)).status).toBe(200);
    expect((await logIn(browser(), VICTIM, OWNER_PASSWORD)).status).toBe(302);
  });

  it('a stale verification link cannot touch it, even from a browser that holds a claim for the address', async () => {
    await verifiedOwner();
    const before = { ...account(VICTIM) };
    // A leftover, unused link for the (now verified) account.
    const raw = crypto.randomBytes(32).toString('hex');
    verificationTokens.push({ customer_id: before.id, token: sha256(raw), used: false });

    const attacker = browser();
    await signUp(attacker, VICTIM, ATTACKER_PASSWORD, 'Mallory');
    const res = await attacker.get(`/verify-email?token=${raw}`);

    expect(res.text).toContain('expired or already been used');
    expect(account(VICTIM)).toEqual(before);
  });
});

describe('the signup form gives nothing away', () => {
  it('answers the same for a free address, an unverified one and a verified one', async () => {
    // unverified: someone claimed it earlier
    await signUp(browser(), 'unverified@example.test', ATTACKER_PASSWORD, 'Earlier');
    // verified
    const owner = browser();
    await signUp(owner, 'verified@example.test', OWNER_PASSWORD, 'Owner');
    await owner.get(latestVerificationLink());

    const outcomes = [];
    for (const email of ['free@example.test', 'unverified@example.test', 'verified@example.test']) {
      const res = await signUp(browser(), email, 'another-password-1', 'Somebody');
      outcomes.push({
        status: res.status,
        location: res.headers.location,
        setsSessionCookie: /cas\.sid=/.test(String(res.headers['set-cookie'] || '')),
        bodyLength: res.text.length,
      });
    }
    expect(outcomes[0]).toEqual({ status: 302, location: '/verify-email-sent', setsSessionCookie: true, bodyLength: outcomes[0].bodyLength });
    expect(outcomes[1]).toEqual(outcomes[0]);
    expect(outcomes[2]).toEqual(outcomes[0]);
  });

  it('hashes the password once in each of the three cases (same work before the lookup)', async () => {
    await signUp(browser(), 'unverified@example.test', ATTACKER_PASSWORD, 'Earlier');
    const owner = browser();
    await signUp(owner, 'verified@example.test', OWNER_PASSWORD, 'Owner');
    await owner.get(latestVerificationLink());

    const hashSpy = vi.spyOn(bcrypt, 'hash');
    try {
      for (const email of ['free@example.test', 'unverified@example.test', 'verified@example.test']) {
        hashSpy.mockClear();
        statements = [];
        await signUp(browser(), email, 'another-password-1', 'Somebody');
        expect(hashSpy, email).toHaveBeenCalledTimes(1);
        // The hash is computed before the first statement runs.
        expect(statements[0], email).toMatch(/^SELECT id, email_verified FROM customers/);
      }
    } finally {
      hashSpy.mockRestore();
    }
  });

  it('keeps the address out of every redirect URL and shows it from the session instead', async () => {
    const agent = browser();
    const signup = await signUp(agent, 'Private.Person@Example.Test', OWNER_PASSWORD, 'Private Person');
    expect(signup.headers.location).toBe('/verify-email-sent');

    const page = await agent.get('/verify-email-sent');
    expect(page.text).toContain('private.person@example.test');

    const resend = await agent.post('/resend-verification').type('form').send({ _csrf: csrf(), email: 'private.person@example.test' });
    expect(resend.headers.location).toBe('/verify-email-sent');

    // Someone else's browser, with the address pasted into the URL, is shown
    // nothing of it, and is asked for an address instead of being handed a
    // resend button that would send nothing.
    const other = await browser().get('/verify-email-sent?email=private.person%40example.test');
    expect(other.text).not.toContain('private.person@example.test');
    expect(other.text).toMatch(/<input type="email" name="email" required/);
    expect(page.text).not.toMatch(/<input type="email" name="email" required/);
    expect(page.text).toContain('<input type="hidden" name="email" value="private.person@example.test" />');
  });
});

describe('finishing on another device', () => {
  it('signing up in one browser and opening the link in another asks for a password instead of trusting the stored one', async () => {
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');
    const res = await browser().get(latestVerificationLink());
    expect(res.headers.location).toMatch(/^\/reset-password\?token=/);
    expect(account(VICTIM).email_verified).toBe(true);
  });

  it('logging in with the right password in the second browser records the claim there, so a link opened there keeps that password and name', async () => {
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');
    const second = browser();

    const login = await logIn(second, VICTIM, OWNER_PASSWORD);
    expect(login.status).toBe(200); // not verified yet: the "verify first" page
    expect(login.text).toContain('/verify-email-sent');
    expect(login.text).not.toContain('/verify-email-sent?email=');

    await second.post('/resend-verification').type('form').send({ _csrf: csrf(), email: VICTIM });
    const verified = await second.get(latestVerificationLink());
    expect(verified.headers.location).toBe('/intake');
    expect(account(VICTIM).name).toBe('Vera Owner');
    expect((await logIn(browser(), VICTIM, OWNER_PASSWORD)).status).toBe(302);
  });

  it('a wrong password records no claim', async () => {
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');
    const second = browser();
    await logIn(second, VICTIM, 'not-the-password');
    const res = await second.get(latestVerificationLink());
    expect(res.headers.location).toMatch(/^\/reset-password\?token=/);
  });
});

describe('a completed password reset proves the mailbox too', () => {
  it('an unverified account (as Harbor creates for a payer) can set a password by reset and log in straight away', async () => {
    customers.push({
      id: 'cust-harbor',
      email: 'payer@example.test',
      password: await bcrypt.hash(crypto.randomBytes(16).toString('hex'), 4),
      name: 'payer',
      email_verified: false,
    });
    const agent = browser();
    await agent.post('/forgot-password').type('form').send({ _csrf: csrf(), email: 'payer@example.test' });
    const link = latestResetLink();
    const token = new URL(link, 'http://localhost').searchParams.get('token') as string;

    const set = await agent.post('/reset-password').type('form').send({ _csrf: csrf(), token, password: OWNER_PASSWORD });
    expect(set.text).toContain('Your password is set');
    expect(account('payer@example.test').email_verified).toBe(true);

    const login = await logIn(browser(), 'payer@example.test', OWNER_PASSWORD);
    expect(login.status).toBe(302);
    expect(login.headers.location).toBe('/dashboard');
  });

  it('retires verification links that were still outstanding for the account', async () => {
    await signUp(browser(), VICTIM, ATTACKER_PASSWORD, 'Mallory');
    const staleLink = latestVerificationLink();
    const owner = browser();
    await owner.post('/forgot-password').type('form').send({ _csrf: csrf(), email: VICTIM });
    const token = new URL(latestResetLink(), 'http://localhost').searchParams.get('token') as string;
    await owner.post('/reset-password').type('form').send({ _csrf: csrf(), token, password: OWNER_PASSWORD });

    expect(verificationTokens.every((t) => t.used)).toBe(true);
    const res = await browser().get(staleLink);
    expect(res.text).toContain('expired or already been used');
    expect((await logIn(browser(), VICTIM, ATTACKER_PASSWORD)).status).toBe(200);
    expect((await logIn(browser(), VICTIM, OWNER_PASSWORD)).status).toBe(302);
  });
});
