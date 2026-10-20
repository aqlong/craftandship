/**
 * In a real browser: the emailed links ask before they act, and a form's CSRF
 * token is good only in the browser whose page it came from.
 *
 *   - A verification link shows a page; the account is verified when its
 *     button is pressed, not when the link is fetched.
 *   - An unsubscribe link shows a page; the address is unsubscribed when its
 *     button is pressed.
 *   - A login form token read from one browser is refused when another browser
 *     submits it, and works in its own.
 *
 * The specs write their own rows (an unverified account and its link, an
 * address to unsubscribe) to the database the suite runs on, under a per-run
 * suffix. The unsubscribe link is signed with SESSION_SECRET, which the server
 * this suite starts shares with it.
 */

import { test, expect, type Page } from '@playwright/test';
import crypto from 'crypto';
import { DATABASE_URL } from './helpers';

const SESSION_SECRET = process.env.SESSION_SECRET || '';
const run = crypto.randomBytes(4).toString('hex');
const sha256 = (value: string): string => crypto.createHash('sha256').update(value).digest('hex');

async function withDb<T>(fn: (pool: { query: (sql: string, params?: unknown[]) => Promise<{ rows: Array<Record<string, unknown>> }> }) => Promise<T>): Promise<T> {
  const { Pool } = require('pg');
  const pool = new Pool({ connectionString: DATABASE_URL });
  try {
    return await fn(pool);
  } finally {
    await pool.end();
  }
}
const flag = (column: 'email_verified' | 'email_unsubscribed', address: string): Promise<boolean> =>
  withDb(async (pool) => (await pool.query(`SELECT ${column} AS value FROM customers WHERE email = $1`, [address])).rows[0].value === true);

test.describe('emailed links ask before they act', () => {
  test.skip(!DATABASE_URL, 'Set DATABASE_URL (the database the suite runs on) so the spec can write its rows');

  test('a verification link shows a page and verifies nothing until its button is pressed', async ({ page }) => {
    const address = `verify-${run}@example.test`;
    const raw = crypto.randomBytes(32).toString('hex');
    await withDb(async (pool) => {
      const { rows } = await pool.query(
        `INSERT INTO customers (email, password, name, email_verified) VALUES ($1, 'not-a-hash', 'Somebody', FALSE) RETURNING id`,
        [address]
      );
      await pool.query('INSERT INTO email_verification_tokens (customer_id, token) VALUES ($1, $2)', [rows[0].id, sha256(raw)]);
    });

    await page.goto(`/verify-email?token=${raw}`);
    await expect(page.locator('h1')).toContainText('Confirm your email.');
    expect(await flag('email_verified', address)).toBe(false);

    await page.click('form[action="/verify-email"] button[type="submit"]');
    // This browser made no claim for the address: it is verified and asked to choose a password.
    await expect(page).toHaveURL(/\/reset-password\?token=[0-9a-f]{64}&verified=1$/);
    await expect(page.locator('body')).toContainText('Your email address is verified. Choose a password');
    expect(await flag('email_verified', address)).toBe(true);
  });

  test('an unsubscribe link shows a page and unsubscribes nobody until its button is pressed', async ({ page }) => {
    test.skip(!SESSION_SECRET, 'Set SESSION_SECRET (the server this suite starts reads the same one) to sign the link');
    const address = `unsubscribe-${run}@example.test`;
    await withDb((pool) =>
      pool.query(`INSERT INTO customers (email, password, name, email_verified) VALUES ($1, 'not-a-hash', 'Somebody', TRUE)`, [address])
    );
    const token = crypto.createHmac('sha256', SESSION_SECRET).update(address).digest('hex');

    await page.goto(`/unsubscribe?email=${encodeURIComponent(address)}&token=${token}`);
    await expect(page.locator('h1')).toContainText('Unsubscribe?');
    expect(await flag('email_unsubscribed', address)).toBe(false);

    await page.click('form[action="/unsubscribe"] button[type="submit"]');
    await expect(page.locator('body')).toContainText("You're off the list.");
    expect(await flag('email_unsubscribed', address)).toBe(true);
  });
});

test.describe('a form token is good only in the browser whose page it came from', () => {
  const tokenField = (page: Page) => page.locator('form[action^="/login"] input[name="_csrf"]');
  const submitLogin = async (page: Page) => {
    await page.fill('input[name="email"]', `nobody-${run}@example.test`);
    await page.fill('input[name="password"]', 'not-the-password-1');
    const [response] = await Promise.all([
      page.waitForNavigation(),
      page.click('form[action^="/login"] button[type="submit"]'),
    ]);
    return response;
  };

  test('a login token from one browser is refused in another, and works in its own', async ({ browser }) => {
    const first = await (await browser.newContext()).newPage();
    const second = await (await browser.newContext()).newPage();
    await first.goto('/login');
    await second.goto('/login');
    const firstToken = await tokenField(first).inputValue();

    // The second browser submits the first browser's token.
    await tokenField(second).evaluate((input, value) => {
      (input as HTMLInputElement).value = value;
    }, firstToken);
    const forged = await submitLogin(second);
    expect(forged?.status()).toBe(403);
    await expect(second.locator('body')).toContainText('This form could not be verified');

    // The first browser's own form reaches the route (the address is unknown, so the login itself fails).
    const own = await submitLogin(first);
    expect(own?.status()).toBe(200);
    await expect(first.locator('body')).toContainText('Invalid email or password.');
  });
});
