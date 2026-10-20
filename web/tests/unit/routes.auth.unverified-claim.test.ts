// Who ends up with the account when an address was claimed before its owner
// proved the mailbox?
//
// These tests drive the real signup, login, verification and reset routes
// through several separate browsers (supertest agents, each with its own
// session) against an in-memory stand-in for the customers and token tables.
// The stand-in implements what each statement is documented to do, which
// makes whole sequences testable (attacker claims, owner claims, somebody
// opens a link). That it matches what Postgres does with the real statements
// is checked separately, by money-path.realdb.test.ts and
// auth-identity.realdb.test.ts.

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
  terms_accepted: boolean;
}
interface Token {
  customer_id: string;
  token: string;
  used: boolean;
  /** Fake-clock minute at which it was issued. */
  issuedAt: number;
}

let customers: Customer[];
let verificationTokens: Token[];
let resetTokens: Token[];
let statements: string[];
/** The stand-in's clock, in minutes. */
let minute: number;

const sha256 = (value: string): string => crypto.createHash('sha256').update(value).digest('hex');

function fakeDatabase(sql: string, params: unknown[]): { rows: unknown[]; rowCount?: number } {
  statements.push(sql);
  const p = params as string[];

  if (/^SELECT id, email_verified FROM customers WHERE email = \$1/.test(sql)) {
    return { rows: customers.filter((c) => c.email === p[0]).map((c) => ({ id: c.id, email_verified: c.email_verified })) };
  }
  if (/INSERT INTO customers \(email, password, name, terms_accepted_at/.test(sql)) {
    if (customers.some((c) => c.email === p[0])) return { rows: [] };
    const row: Customer = { id: `cust-${customers.length + 1}`, email: p[0], password: p[1], name: p[2], email_verified: false, terms_accepted: true };
    customers.push(row);
    return { rows: [{ id: row.id, email: row.email, name: row.name }] };
  }
  if (/^UPDATE email_verification_tokens SET used_at = NOW\(\) WHERE customer_id = \$1 AND used_at IS NULL/.test(sql)) {
    for (const token of verificationTokens) if (token.customer_id === p[0]) token.used = true;
    return { rows: [] };
  }
  // ISSUE_VERIFICATION_TOKEN_SQL: by address; only for an unverified account
  // that was sent no link in the last hour; a row back only when it inserted.
  if (/INSERT INTO email_verification_tokens \(customer_id, token\)\s+SELECT id, \$2 FROM target/.test(sql)) {
    const row = customers.find((c) => c.email === p[0] && !c.email_verified);
    if (!row) return { rows: [] };
    if (verificationTokens.some((t) => t.customer_id === row.id && minute - t.issuedAt < 60)) return { rows: [] };
    verificationTokens.push({ customer_id: row.id, token: p[1], used: false, issuedAt: minute });
    return { rows: [{ customer_id: row.id }] };
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
    if (ownClaim) row.terms_accepted = true;
    return { rows: [{ customer_id: row.id, email: row.email, name: row.name, own_claim: ownClaim }] };
  }
  // ISSUE_PASSWORD_RESET_TOKEN_SQL: by address, for any account.
  if (/INSERT INTO password_reset_tokens \(customer_id, token\)\s+SELECT id, \$2 FROM target/.test(sql)) {
    const row = customers.find((c) => c.email === p[0]);
    if (!row) return { rows: [] };
    resetTokens.push({ customer_id: row.id, token: p[1], used: false, issuedAt: minute });
    return { rows: [{ name: row.name, email_verified: row.email_verified }] };
  }
  // A reset link for a known account (handed over by a verification without a claim).
  if (/INSERT INTO password_reset_tokens \(customer_id, token\)\s+VALUES \(\$1, \$2\)/.test(sql)) {
    resetTokens.push({ customer_id: p[0], token: p[1], used: false, issuedAt: minute });
    return { rows: [] };
  }
  if (/^SELECT id FROM password_reset_tokens\s+WHERE token = \$1/.test(sql)) {
    return { rows: resetTokens.filter((t) => t.token === p[0] && !t.used).map(() => ({ id: 'reset-token' })) };
  }
  if (/UPDATE customers c\s+SET password = \$2, email_verified = TRUE,\s+name = CASE WHEN c\.email_verified IS TRUE THEN c\.name/.test(sql)) {
    const token = resetTokens.find((t) => t.token === p[0] && !t.used);
    if (!token) return { rows: [] };
    token.used = true;
    const row = customers.find((c) => c.id === token.customer_id);
    if (!row) return { rows: [] };
    if (!row.email_verified) row.name = row.email.split('@')[0];
    row.password = p[1];
    row.email_verified = true;
    return { rows: [{ customer_id: row.id }] };
  }
  if (/^SELECT id, email, name, password, email_verified FROM customers WHERE email = \$1/.test(sql)) {
    return { rows: customers.filter((c) => c.email === p[0]).map((c) => ({ ...c })) };
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
/**
 * Open an emailed verification link the way a browser does: the GET shows a
 * page and spends nothing; its button posts the token back with the page's
 * CSRF token (good only in this browser's session).
 */
async function openLink(agent: Browser, link: string) {
  const page = await agent.get(link);
  if (page.status !== 200 || !page.text.includes('action="/verify-email"')) return page;
  const token = (page.text.match(/name="token" value="([^"]+)"/) || [])[1];
  const pageCsrf = (page.text.match(/name="_csrf" value="([^"]+)"/) || [])[1];
  return agent.post('/verify-email').type('form').send({ _csrf: pageCsrf, token });
}
/** The most recent verification link "sent" to the mailbox, as a path. */
function latestVerificationLink(): string {
  const calls = mockSendVerification.mock.calls;
  const url = String(calls[calls.length - 1][1]);
  return url.slice(url.indexOf('/verify-email'));
}
function latestResetLink(): string {
  const calls = mockSendReset.mock.calls;
  const url = String(calls[calls.length - 1][2]);
  return url.slice(url.indexOf('/reset-password'));
}
const account = (email: string): Customer => customers.find((c) => c.email === email) as Customer;
/** Mail starts after the response; wait until the n-th verification mail has gone out. */
async function verificationMails(n: number): Promise<void> {
  await vi.waitFor(() => expect(mockSendVerification).toHaveBeenCalledTimes(n));
}

// Passwords are generated per run; nothing here is a real credential.
const ATTACKER_PASSWORD = `attacker-${crypto.randomBytes(6).toString('hex')}`;
const OWNER_PASSWORD = `owner-${crypto.randomBytes(6).toString('hex')}`;
const VICTIM = 'victim@example.test';

beforeEach(() => {
  customers = [];
  verificationTokens = [];
  resetTokens = [];
  statements = [];
  minute = 0;
  mockQuery.mockReset().mockImplementation((sql: unknown, params?: unknown) =>
    Promise.resolve(fakeDatabase(String(sql).trim(), Array.isArray(params) ? params : []))
  );
  mockSendVerification.mockClear();
  mockSendReset.mockClear();
  mockSendWelcome.mockClear();
});

describe('an address claimed by someone else before its owner signs up', () => {
  it('the owner signs up later and opens the link in the mailbox: their own password and name; the first claimant has nothing', async () => {
    const attacker = browser();
    const owner = browser();

    await signUp(attacker, VICTIM, ATTACKER_PASSWORD, 'Mallory');
    await verificationMails(1);
    const before = { ...account(VICTIM) };

    const ownerSignup = await signUp(owner, VICTIM, OWNER_PASSWORD, 'Vera Owner');
    expect(ownerSignup.status).toBe(302);
    // The owner's signup wrote nothing to the row: the claim waits in the owner's session.
    expect(account(VICTIM)).toEqual(before);
    // The address was sent a link less than an hour ago, so no second one goes out;
    // the one in the mailbox still works.
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendVerification).toHaveBeenCalledTimes(1);

    const verified = await openLink(owner, latestVerificationLink());
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

  it('a stranger\'s signup for an unverified account writes nothing to it and spends none of its links', async () => {
    // The owner's own account: created by their signup, a link outstanding.
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await verificationMails(1);
    const before = { ...account(VICTIM) };
    statements = [];

    const res = await signUp(browser(), VICTIM, ATTACKER_PASSWORD, 'URGENT: your card was declined');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/verify-email-sent');

    // No password, name or terms change, and no link spent.
    expect(account(VICTIM)).toEqual(before);
    expect(verificationTokens.every((t) => !t.used)).toBe(true);
    expect(statements.filter((sql) => /^UPDATE/.test(sql))).toEqual([]);
    expect(statements.filter((sql) => /INSERT INTO customers/.test(sql))).toEqual([]);
    // Nor does the stranger get a mail sent there (one link an hour), and no mail carries a typed name.
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendVerification).toHaveBeenCalledTimes(1);
    expect(mockSendVerification.mock.calls[0]).toHaveLength(2);
    expect(JSON.stringify(mockSendVerification.mock.calls)).not.toContain('URGENT');
  });

  it('the first claimant claims AGAIN after the owner; the owner opens the link and still gets their own password', async () => {
    const attacker = browser();
    const owner = browser();
    await signUp(owner, VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await verificationMails(1);
    await signUp(attacker, VICTIM, ATTACKER_PASSWORD, 'Mallory');
    // The row still carries the owner's claim, and the only link is the one the owner's signup sent.
    expect(await bcrypt.compare(OWNER_PASSWORD, account(VICTIM).password)).toBe(true);
    const link = latestVerificationLink();

    const verified = await openLink(owner, link);
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
    await verificationMails(1);
    const attackerHash = account(VICTIM).password;
    const link = latestVerificationLink();

    const owner = browser();
    const verified = await openLink(owner, link);
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

  it('opening the link spends nothing: a mail scanner fetching it leaves it for the owner\'s own browser', async () => {
    const owner = browser();
    await signUp(owner, VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await verificationMails(1);
    const link = latestVerificationLink();

    // A scanner (no claim, no cookies) fetches the link.
    const scanned = await request(app).get(link);
    expect(scanned.status).toBe(200);
    expect(account(VICTIM).email_verified).toBe(false);
    expect(verificationTokens.every((t) => !t.used)).toBe(true);

    const verified = await openLink(owner, link);
    expect(verified.headers.location).toBe('/intake');
    expect(account(VICTIM).name).toBe('Vera Owner');
  });
});

describe('verification mail: at most one link an hour per address, whoever asks', () => {
  it('a second request within the hour sends nothing; after the hour one more goes out', async () => {
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await verificationMails(1);

    for (const asker of [browser(), browser()]) {
      await asker.post('/resend-verification').type('form').send({ _csrf: csrf(), email: VICTIM });
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendVerification).toHaveBeenCalledTimes(1);
    expect(verificationTokens).toHaveLength(1);

    minute = 61;
    await browser().post('/resend-verification').type('form').send({ _csrf: csrf(), email: VICTIM });
    await verificationMails(2);
    expect(verificationTokens).toHaveLength(2);
    // The earlier link was not voided by the new one.
    expect(verificationTokens.every((t) => !t.used)).toBe(true);
  });
});

describe('opening a link clears only the claim it settled (seca-8)', () => {
  it('a browser holding a claim for B opens a link for A: B\'s claim survives and B\'s link then logs it in', async () => {
    const A = 'a-person@example.test';
    const B = 'b-person@example.test';
    // A's account exists, with a link in A's mailbox; this browser never claimed A.
    await signUp(browser(), A, ATTACKER_PASSWORD, 'Person A');
    await verificationMails(1);
    const linkA = latestVerificationLink();

    const b = browser();
    await signUp(b, B, OWNER_PASSWORD, 'Person B');
    await verificationMails(2);
    const linkB = latestVerificationLink();

    const openedA = await openLink(b, linkA);
    expect(openedA.headers.location).toMatch(/^\/reset-password\?token=/);

    const openedB = await openLink(b, linkB);
    expect(openedB.status).toBe(302);
    expect(openedB.headers.location).toBe('/intake');
    expect(account(B).name).toBe('Person B');
    expect(await bcrypt.compare(OWNER_PASSWORD, account(B).password)).toBe(true);
  });
});

describe('a verified account is its owner\'s', () => {
  async function verifiedOwner(): Promise<void> {
    const owner = browser();
    await signUp(owner, VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await verificationMails(1);
    await openLink(owner, latestVerificationLink());
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
    verificationTokens.push({ customer_id: before.id, token: sha256(raw), used: false, issuedAt: minute });

    const attacker = browser();
    await signUp(attacker, VICTIM, ATTACKER_PASSWORD, 'Mallory');
    const page = await attacker.get(`/verify-email?token=${raw}`);
    expect(page.text).toContain('expired or already been used');
    // And straight to the POST, with a token from a page of this browser.
    const pageCsrf = (page.text.match(/name="_csrf" value="([^"]+)"/) || [])[1];
    const res = await attacker.post('/verify-email').type('form').send({ _csrf: pageCsrf, token: raw });

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
    await verificationMails(2);
    await openLink(owner, latestVerificationLink());

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
    await verificationMails(2);
    await openLink(owner, latestVerificationLink());

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
    await verificationMails(1);
    const res = await openLink(browser(), latestVerificationLink());
    expect(res.headers.location).toMatch(/^\/reset-password\?token=/);
    expect(account(VICTIM).email_verified).toBe(true);
  });

  it('logging in with the right password in the second browser records the claim there, so a link opened there keeps that password and name', async () => {
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await verificationMails(1);
    const second = browser();

    const login = await logIn(second, VICTIM, OWNER_PASSWORD);
    expect(login.status).toBe(200); // not verified yet: the "verify first" page
    expect(login.text).toContain('/verify-email-sent');
    expect(login.text).not.toContain('/verify-email-sent?email=');

    // Asking again within the hour sends nothing new; the link already in the mailbox works.
    await second.post('/resend-verification').type('form').send({ _csrf: csrf(), email: VICTIM });
    const verified = await openLink(second, latestVerificationLink());
    expect(verified.headers.location).toBe('/intake');
    expect(account(VICTIM).name).toBe('Vera Owner');
    expect((await logIn(browser(), VICTIM, OWNER_PASSWORD)).status).toBe(302);
  });

  it('a wrong password records no claim', async () => {
    await signUp(browser(), VICTIM, OWNER_PASSWORD, 'Vera Owner');
    await verificationMails(1);
    const second = browser();
    await logIn(second, VICTIM, 'not-the-password');
    const res = await openLink(second, latestVerificationLink());
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
      terms_accepted: false,
    });
    const agent = browser();
    await agent.post('/forgot-password').type('form').send({ _csrf: csrf(), email: 'payer@example.test' });
    await vi.waitFor(() => expect(mockSendReset).toHaveBeenCalledTimes(1));
    const link = latestResetLink();
    const token = new URL(link, 'http://localhost').searchParams.get('token') as string;

    const set = await agent.post('/reset-password').type('form').send({ _csrf: csrf(), token, password: OWNER_PASSWORD });
    expect(set.text).toContain('Your password is set');
    expect(account('payer@example.test').email_verified).toBe(true);

    const login = await logIn(browser(), 'payer@example.test', OWNER_PASSWORD);
    expect(login.status).toBe(302);
    expect(login.headers.location).toBe('/dashboard');
  });

  it('retires verification links that were still outstanding for the account, and drops the name a stranger typed', async () => {
    await signUp(browser(), VICTIM, ATTACKER_PASSWORD, 'URGENT: your card was declined');
    await verificationMails(1);
    const staleLink = latestVerificationLink();
    const owner = browser();
    await owner.post('/forgot-password').type('form').send({ _csrf: csrf(), email: VICTIM });
    await vi.waitFor(() => expect(mockSendReset).toHaveBeenCalledTimes(1));
    // The reset mail greets nobody by name: the account is not verified, so its name is a stranger's.
    expect(mockSendReset.mock.calls[0][1]).toBeNull();
    const token = new URL(latestResetLink(), 'http://localhost').searchParams.get('token') as string;
    await owner.post('/reset-password').type('form').send({ _csrf: csrf(), token, password: OWNER_PASSWORD });

    expect(verificationTokens.every((t) => t.used)).toBe(true);
    expect(account(VICTIM).name).toBe('victim');
    const res = await browser().get(staleLink);
    expect(res.text).toContain('expired or already been used');
    expect((await logIn(browser(), VICTIM, ATTACKER_PASSWORD)).status).toBe(200);
    expect((await logIn(browser(), VICTIM, OWNER_PASSWORD)).status).toBe(302);
  });
});
