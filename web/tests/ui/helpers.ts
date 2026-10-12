import { expect, type Page } from '@playwright/test';
import { uiOrigin } from './port';

const { seedUiFixtures, LAMP_BUILD_ID, LIVE_BUILD_ID } = require('./fixtures');

export { LAMP_BUILD_ID, LIVE_BUILD_ID };

export const TEST_EMAIL = process.env.DEV_TEST_EMAIL || '';
export const TEST_PASSWORD = process.env.DEV_TEST_PASSWORD || '';
// Matches use.baseURL in playwright.config.ts (both read PORT, default 4000).
const BASE_ORIGIN = uiOrigin();

export const DATABASE_URL = process.env.DATABASE_URL || '';

/** Why the browser specs cannot run, or '' when they can. Used as a test.skip reason. */
export function missingFixtureReason(): string {
  if (!TEST_EMAIL || !TEST_PASSWORD) return 'Set DEV_TEST_EMAIL and DEV_TEST_PASSWORD (the seeded test user) to run this spec';
  if (!DATABASE_URL) return 'Set DATABASE_URL (the database tests/ui/seed-test-db.js seeded) so the spec can apply its fixture rows';
  return '';
}

/** Re-apply the fixture rows (idempotent), so the spec does not depend on the seed having just run. */
export async function ensureFixtures(): Promise<void> {
  const { Pool } = require('pg');
  const pool = new Pool({ connectionString: DATABASE_URL });
  try {
    await seedUiFixtures(pool, TEST_EMAIL);
  } finally {
    await pool.end();
  }
}

export async function loginAsTestUser(page: Page): Promise<void> {
  await page.goto('/login');
  await page.fill('input[name="email"]', TEST_EMAIL);
  await page.fill('input[name="password"]', TEST_PASSWORD);
  await page.click('form[action^="/login"] button[type="submit"]');
  await expect(page).toHaveURL('/dashboard');
}

export interface BrowserProblems {
  /** console errors and warnings, uncaught page errors, CSP violations and failed same-origin requests */
  list: () => string[];
}

/**
 * Record everything that would show up red in a developer's console on this page:
 * console errors and warnings (those about other origins excluded), uncaught
 * exceptions, Content-Security-Policy violations, and same-origin requests that
 * failed or answered 4xx/5xx. Call before the first navigation.
 */
export async function collectBrowserProblems(page: Page): Promise<BrowserProblems> {
  const problems: string[] = [];
  const origin = BASE_ORIGIN;
  page.on('console', (message) => {
    if (message.type() !== 'error' && message.type() !== 'warning') return;
    const where = message.location().url || '';
    if (where && !where.startsWith(origin) && !where.startsWith('about:') && !where.startsWith('data:')) return;
    problems.push(`console.${message.type()}: ${message.text().slice(0, 200)}`);
  });
  page.on('pageerror', (error) => problems.push(`pageerror: ${String(error).slice(0, 200)}`));
  page.on('requestfailed', (request) => {
    if (request.url().startsWith(origin)) problems.push(`requestfailed: ${request.url()} ${request.failure()?.errorText ?? ''}`);
  });
  page.on('response', (response) => {
    if (response.url().startsWith(origin) && response.status() >= 400) problems.push(`HTTP ${response.status()}: ${response.url()}`);
  });
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      // Surfaced through the console so the listener above collects it.
      console.error(`CSP violation: ${event.violatedDirective} blocked ${event.blockedURI}`);
    });
  });
  return { list: () => problems.slice() };
}
