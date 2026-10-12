/**
 * Behavior that only a real browser can prove, after the review rounds replaced
 * inline event handlers with delegated listeners, tightened the
 * Content-Security-Policy and changed the auth flows:
 *
 *   - the status and dashboard buttons that used to be onclick="..." attributes;
 *   - the Harbor price slider on the make-public page and the amount it submits;
 *   - the cancel-subscription confirmation;
 *   - no console error and no CSP violation on the main public pages and the
 *     Harbor table page;
 *   - login, logout and the forgot-password page never showing a reset link.
 *
 * No model call is made and nothing leaves the machine: the two POSTs that would
 * reach Stripe (make-public, cancel-subscription) are intercepted in the browser
 * and answered with a stub, after the test has read what was submitted.
 */

import { test, expect } from '@playwright/test';
import {
  LAMP_BUILD_ID,
  LIVE_BUILD_ID,
  TEST_EMAIL,
  TEST_PASSWORD,
  collectBrowserProblems,
  ensureFixtures,
  loginAsTestUser,
  missingFixtureReason,
} from './helpers';

test.describe('delegated click handlers (status page and dashboard)', () => {
  test.skip(!!missingFixtureReason(), missingFixtureReason());
  test.beforeAll(ensureFixtures);
  test.beforeEach(async ({ page }) => {
    await loginAsTestUser(page);
  });

  test('status page: Rebuild with changes opens the dialog; Cancel and a click outside close it', async ({ page }) => {
    await page.goto(`/builds/${LIVE_BUILD_ID}`);
    const dialog = page.locator('#rebuild-modal');
    await expect(dialog).toBeHidden();

    await page.getByRole('button', { name: 'Rebuild with changes' }).first().click();
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('form')).toHaveAttribute('action', `/builds/${LIVE_BUILD_ID}/rebuild`);

    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden();

    await page.getByRole('button', { name: 'Rebuild with changes' }).first().click();
    await expect(dialog).toBeVisible();
    await dialog.click({ position: { x: 4, y: 4 } }); // the backdrop, outside the panel
    await expect(dialog).toBeHidden();
  });

  test('status page: a screenshot opens in the lightbox; Escape and the close button dismiss it', async ({ page }) => {
    await page.goto(`/builds/${LIVE_BUILD_ID}`);
    const lightbox = page.locator('#screenshot-lightbox');
    await expect(lightbox).toBeHidden();

    await page.locator('.status-shots__cell').first().click();
    await expect(lightbox).toBeVisible();
    await expect(page.locator('#screenshot-lightbox-img')).toHaveAttribute('src', new RegExp(`/api/builds/${LIVE_BUILD_ID}/screenshots/0$`));
    await page.keyboard.press('Escape');
    await expect(lightbox).toBeHidden();

    await page.locator('.status-shots__cell').first().click();
    await expect(lightbox).toBeVisible();
    await page.getByRole('button', { name: 'Close screenshot' }).click();
    await expect(lightbox).toBeHidden();
  });

  test('status page: Send feedback posts the survey and says thanks', async ({ page }) => {
    await page.goto(`/builds/${LIVE_BUILD_ID}`);
    const posted = page.waitForResponse((response) => response.url().endsWith('/feedback') && response.request().method() === 'POST');
    await page.fill('#survey-unanswered', 'Ordinary question from the browser test.');
    await page.click('#survey-submit');
    expect((await posted).status()).toBe(200);
    await expect(page.locator('#survey-status')).toBeVisible();
    await expect(page.locator('#survey-submit')).toHaveText('Sent');
  });

  test('dashboard: Rebuild opens the dialog for that build; Cancel closes it', async ({ page }) => {
    await page.goto('/dashboard');
    const dialog = page.locator('#rebuild-modal');
    await expect(dialog).toBeHidden();

    await page.locator(`[data-rebuild-open="${LIVE_BUILD_ID}"]`).click();
    await expect(dialog).toBeVisible();
    await expect(page.locator('#rebuild-form')).toHaveAttribute('action', `/builds/${LIVE_BUILD_ID}/rebuild`);

    await dialog.locator('[data-rebuild-close]').click();
    await expect(dialog).toBeHidden();
  });
});

test.describe('make-public: the price slider sets the amount that is submitted', () => {
  test.skip(!!missingFixtureReason(), missingFixtureReason());
  test.beforeAll(ensureFixtures);

  test('moving the slider updates the display, asks for the attestation below $39, and submits that amount', async ({ page }) => {
    await loginAsTestUser(page);
    await page.goto(`/lighthouse/lamp/${LAMP_BUILD_ID}/make-public`);

    const slider = page.locator('#amount-slider');
    const display = page.locator('#amount-display');
    const attestation = page.locator('#attestation-block');
    await expect(slider).toHaveValue('3900');
    await expect(display).toHaveText('$39');
    await expect(attestation).toBeHidden();

    // Stop the request before it can reach Stripe, and keep what was submitted.
    let submitted = '';
    await page.route('**/make-public', async (route) => {
      if (route.request().method() === 'POST') {
        submitted = route.request().postData() || '';
        await route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>stub</title><p>stub</p>' });
      } else {
        await route.continue();
      }
    });

    await slider.fill('2900');
    await expect(display).toHaveText('$29');
    await expect(attestation).toBeVisible();

    // A logged-in owner's address is shown as text, not asked for: there is no field to type into.
    await expect(page.locator('#account-email')).toHaveText(TEST_EMAIL);
    await expect(page.locator('input[type="email"]')).toHaveCount(0);
    // Below the standard amount the attestation is required: the browser refuses to submit without it.
    await page.getByRole('button', { name: /Make it public/ }).click();
    await expect(page).toHaveURL(new RegExp(`/lighthouse/lamp/${LAMP_BUILD_ID}/make-public$`));
    expect(submitted).toBe('');

    await page.check('#att');
    await page.getByRole('button', { name: /Make it public/ }).click();
    await expect.poll(() => submitted).not.toBe('');
    const body = new URLSearchParams(submitted);
    expect(body.get('monthly_amount_cents')).toBe('2900');
    expect(body.get('sliding_scale_attestation')).toBe('on');
    expect(body.get('email')).toBe(TEST_EMAIL);
    expect(body.get('_csrf')).toBeTruthy();
  });

  test('back at $39 the attestation is hidden again and the standard amount is submitted', async ({ page }) => {
    await loginAsTestUser(page);
    await page.goto(`/lighthouse/lamp/${LAMP_BUILD_ID}/make-public`);

    let submitted = '';
    await page.route('**/make-public', async (route) => {
      if (route.request().method() === 'POST') {
        submitted = route.request().postData() || '';
        await route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>stub</title>' });
      } else {
        await route.continue();
      }
    });

    const slider = page.locator('#amount-slider');
    await slider.fill('1900');
    await expect(page.locator('#amount-display')).toHaveText('$19');
    await expect(page.locator('#attestation-block')).toBeVisible();
    await slider.fill('3900');
    await expect(page.locator('#amount-display')).toHaveText('$39');
    await expect(page.locator('#attestation-block')).toBeHidden();

    await page.getByRole('button', { name: /Make it public/ }).click();
    await expect.poll(() => submitted).not.toBe('');
    expect(new URLSearchParams(submitted).get('monthly_amount_cents')).toBe('3900');
  });
});

test.describe('cancel subscription asks first', () => {
  test.skip(!!missingFixtureReason(), missingFixtureReason());
  test.beforeAll(ensureFixtures);

  test('dismissing the confirmation sends nothing; accepting it submits the cancellation form', async ({ page }) => {
    await loginAsTestUser(page);
    await page.goto(`/lighthouse/lamp/${LIVE_BUILD_ID}`);

    let posts = 0;
    let postedBody = '';
    await page.route('**/cancel-subscription', async (route) => {
      if (route.request().method() === 'POST') {
        posts += 1;
        postedBody = route.request().postData() || '';
        await route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>stub</title>' });
      } else {
        await route.continue();
      }
    });

    const messages: string[] = [];
    let accept = false;
    page.on('dialog', async (dialog) => {
      messages.push(dialog.message());
      if (accept) await dialog.accept();
      else await dialog.dismiss();
    });

    const cancel = page.getByRole('button', { name: 'Cancel subscription' });
    await expect(cancel).toBeVisible();

    await cancel.click();
    await expect.poll(() => messages.length).toBe(1);
    expect(messages[0]).toMatch(/website stays up/i);
    await page.waitForTimeout(300);
    expect(posts).toBe(0);
    await expect(page).toHaveURL(new RegExp(`/lighthouse/lamp/${LIVE_BUILD_ID}$`));

    accept = true;
    await cancel.click();
    await expect.poll(() => posts).toBe(1);
    expect(new URLSearchParams(postedBody).get('_csrf')).toBeTruthy();
  });
});

test.describe('no console error and no CSP violation on the public pages', () => {
  // External hosts (fonts, analytics) are not reachable on every runner and are
  // not under test: only this server's own responses and the page's own scripts are.
  const PAGES = [
    '/', '/pricing', '/how-it-works', '/the-companion', '/gallery', '/about', '/signup', '/login',
    '/forgot-password', '/lighthouse/table', '/lighthouse/table/recall', '/lighthouse/preview',
  ];

  for (const path of PAGES) {
    test(`${path} loads clean`, async ({ page }) => {
      const problems = await collectBrowserProblems(page);
      const response = await page.goto(path, { waitUntil: 'networkidle' });
      expect(response?.status(), `status of ${path}`).toBe(200);
      expect(problems.list()).toEqual([]);
    });
  }
});

test.describe('login, logout and the forgot-password page', () => {
  test.skip(!!missingFixtureReason(), missingFixtureReason());
  test.beforeAll(ensureFixtures);

  test('login lands on the dashboard; logout ends the session and offers sign-in again; no console errors', async ({ page }) => {
    const problems = await collectBrowserProblems(page);
    await loginAsTestUser(page);
    await expect(page.getByRole('heading').first()).toBeVisible();

    // The logout control is a form post (a link would be a GET that any page could trigger).
    await page.locator('form[action="/logout"] button:visible').first().click();
    await expect(page).toHaveURL('/');
    // Logged out: the account links are gone and the sign-in link is back.
    await expect(page.locator('a[href="/dashboard"]')).toHaveCount(0);
    await expect(page.locator('a[href="/login"]').first()).toBeAttached();
    // ... and the session really ended: the dashboard now asks for a login.
    await page.goto('/dashboard');
    await expect(page).toHaveURL(/\/login/);
    expect(problems.list()).toEqual([]);
  });

  test('forgot-password for an existing account says "check your inbox" and shows no reset link', async ({ page }) => {
    const problems = await collectBrowserProblems(page);
    await page.goto('/forgot-password');
    await page.fill('input[name="email"]', TEST_EMAIL);
    await page.click('form[action="/forgot-password"] button[type="submit"]');
    await expect(page.getByText(/check your inbox/i)).toBeVisible();

    // The link is proof of mailbox control and travels by email only: not in
    // the rendered text, the markup, any attribute, or the address bar.
    const html = await page.content();
    expect(html).not.toMatch(/reset-password\?token=/);
    expect(html).not.toMatch(/[?&]token=[0-9a-f]{20,}/);
    expect(page.url()).not.toContain('token=');
    expect(await page.locator('a[href*="reset-password"]').count()).toBe(0);
    expect(problems.list()).toEqual([]);
  });

  test('forgot-password for an unknown address looks the same to the visitor', async ({ page }) => {
    await page.goto('/forgot-password');
    await page.fill('input[name="email"]', `nobody-${Date.now()}@example.com`);
    await page.click('form[action="/forgot-password"] button[type="submit"]');
    await expect(page.getByText(/check your inbox/i)).toBeVisible();
    expect(await page.content()).not.toMatch(/reset-password\?token=/);
  });

  test('the password the form was filled with never appears in the page after a failed login', async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="email"]', TEST_EMAIL);
    await page.fill('input[name="password"]', `${TEST_PASSWORD}-wrong`);
    await page.click('form[action^="/login"] button[type="submit"]');
    await expect(page).toHaveURL(/\/login/);
    expect(await page.content()).not.toContain(`${TEST_PASSWORD}-wrong`);
  });
});
