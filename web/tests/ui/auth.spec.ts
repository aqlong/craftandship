import { test, expect } from '@playwright/test';

// These tests run against a live instance of the app with a real DB.
// Use unique timestamped emails to avoid conflicts between test runs.

const timestamp = Date.now();

test.describe('Signup flow', () => {
  test('navigate to /signup, fill form, submit, land on verify-email-sent', async ({ page }) => {
    const email = `testuser_${timestamp}@example.com`;

    await page.goto('/signup');
    await expect(page).toHaveTitle(/Create Account/);

    await page.fill('input[name="email"]', email);
    await page.fill('input[name="password"]', 'local-dev-password');
    await page.fill('input[name="name"]', 'Test User');
    await page.click('button[type="submit"]');

    // After signup, user is redirected to /verify-email-sent (email verification required)
    await expect(page).toHaveURL(/\/verify-email-sent/);
    await expect(page.locator('body')).toContainText(/check your|verify/i);
  });
});

test.describe('Admin email log: unsubscribe link in email previews', () => {
  // This test requires admin credentials and at least one sent email in the log.
  // Set ADMIN_TEST_EMAIL and ADMIN_TEST_PASSWORD to run.
  const adminEmail = process.env.ADMIN_TEST_EMAIL;
  const adminPassword = process.env.ADMIN_TEST_PASSWORD;

  test.skip(!adminEmail || !adminPassword, 'Set ADMIN_TEST_EMAIL and ADMIN_TEST_PASSWORD to run admin email preview tests');

  test('admin email detail page contains an unsubscribe link in the email body preview', async ({ page }) => {
    // Log in as admin
    await page.goto('/login');
    await page.fill('input[name="email"]', adminEmail!);
    await page.fill('input[name="password"]', adminPassword!);
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL('/dashboard');

    // Navigate to the email log (the page title is "Email Log"; the h1 is "Admin")
    await page.goto('/admin/emails');
    await expect(page).toHaveTitle(/Email Log/);

    // Find a customer-facing email that's known to carry an unsubscribe link
    // (welcome, build_queued, preview_ready, app_live; NOT email_verification or
    // password_reset which are transactional). In CI the only sent emails come
    // from the signup flow and are email_verification, so fall back to skipping
    // when none of the customer templates have any rows. Unit-level coverage in
    // tests/unit/email.service.test.ts asserts the unsubscribe link generation
    // itself; this UI test only verifies it survives admin preview rendering.
    const templatesWithUnsubscribe = ['welcome', 'build_queued', 'preview_ready', 'app_live'];
    let foundEmailId: string | null = null;
    for (const template of templatesWithUnsubscribe) {
      await page.goto(`/admin/emails?template=${template}&status=sent`);
      // Each row is a <tr> with an inner <a href="/admin/emails/<uuid>"> in the
      // subject column. Take the first matching anchor's href as the detail URL.
      const rowLink = page.locator('table tbody tr a[href^="/admin/emails/"]').first();
      const rowCount = await rowLink.count();
      if (rowCount > 0) {
        const href = await rowLink.getAttribute('href');
        if (href) {
          foundEmailId = href;
          break;
        }
      }
    }
    test.skip(!foundEmailId, 'No customer-facing emails (welcome/build_queued/preview_ready/app_live) in log; unit tests cover the link generation');

    // Open the detail view and verify the iframe preview carries the unsubscribe link
    await page.goto(foundEmailId!);
    await expect(page).toHaveURL(/\/admin\/emails\/[0-9a-f-]+/);

    const iframe = page.locator('iframe#email-frame');
    await expect(iframe).toBeVisible();
    const srcdoc = await iframe.getAttribute('srcdoc');
    expect(srcdoc).toMatch(/unsubscribe/i);
  });
});

test.describe('Login flow', () => {
  // This test depends on a known account existing in the dev database.
  // Set DEV_TEST_EMAIL and DEV_TEST_PASSWORD env vars for a pre-seeded account,
  // or skip if not configured.
  const testEmail = process.env.DEV_TEST_EMAIL;
  const testPassword = process.env.DEV_TEST_PASSWORD;

  test.skip(!testEmail || !testPassword, 'Set DEV_TEST_EMAIL and DEV_TEST_PASSWORD to run login tests');

  test('login with known credentials lands on /dashboard', async ({ page }) => {
    await page.goto('/login');
    await expect(page).toHaveTitle(/Log In/);

    await page.fill('input[name="email"]', testEmail!);
    await page.fill('input[name="password"]', testPassword!);
    await page.click('button[type="submit"]');

    await expect(page).toHaveURL('/dashboard');
  });

  test('login with wrong password shows error and stays on /login', async ({ page }) => {
    await page.goto('/login');

    await page.fill('input[name="email"]', testEmail!);
    await page.fill('input[name="password"]', 'definitelywrongpassword');
    await page.click('button[type="submit"]');

    await expect(page).toHaveURL(/\/login/);
    await expect(page.locator('body')).toContainText('Invalid email or password');
  });

  test('logout redirects to home and shows Log In link', async ({ page }) => {
    // First log in
    await page.goto('/login');
    await page.fill('input[name="email"]', testEmail!);
    await page.fill('input[name="password"]', testPassword!);
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL('/dashboard');

    // The harbor layout collapses the nav into a hamburger panel below 1280px.
    // If the toggle is visible, open it first so the Sign out button is reachable.
    const navToggle = page.locator('button.nav-toggle');
    if (await navToggle.isVisible()) {
      await navToggle.click();
    }

    // Click the Sign out button. Both the inline nav and the footer have one;
    // .first() picks the (now-visible) nav-panel button.
    await page.locator('button:has-text("Sign out")').first().click();

    // Should be redirected to home
    await expect(page).toHaveURL('/');

    // The Sign in link lives in the nav. At narrow widths it's inside the
    // hamburger panel; at wide widths it's inline. Either way, we just need
    // to confirm the user is logged out, which we can also do by checking
    // that no logged-in-only nav items exist (My websites, Sign out).
    await expect(page.locator('a[href="/dashboard"]').first()).toHaveCount(0);
    await expect(page.locator('button:has-text("Sign out")').first()).toHaveCount(0);

    // And the Sign in link IS in the DOM (visible inline at wide; in the
    // hamburger panel at narrow: open it if needed to verify visibility).
    const homeNavToggle = page.locator('button.nav-toggle');
    if (await homeNavToggle.isVisible()) {
      await homeNavToggle.click();
    }
    await expect(page.locator('a[href="/login"]').first()).toBeVisible();
  });
});
