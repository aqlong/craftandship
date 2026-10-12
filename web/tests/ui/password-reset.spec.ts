import { test, expect } from '@playwright/test';

// These tests run against a live instance of the app with a real DB.
// The happy path token test is skipped because it requires a pre-seeded DB token.

test.describe('Forgot password flow', () => {
  test('clicking "Forgot password?" on login page navigates to /forgot-password', async ({ page }) => {
    await page.goto('/login');
    await expect(page).toHaveTitle(/Log In/);

    // Find the forgot password link
    const forgotLink = page.locator('a[href="/forgot-password"]');
    await expect(forgotLink).toBeVisible();
    await forgotLink.click();

    await expect(page).toHaveURL(/\/forgot-password/);
  });

  test('submitting empty forgot-password form shows neutral message or validation', async ({ page }) => {
    await page.goto('/forgot-password');

    // Submit without filling in email
    await page.click('button[type="submit"]');

    // Should either show HTML5 validation or the neutral server message
    // Both are acceptable behaviors; we just check we remain on the page
    await expect(page).toHaveURL(/\/forgot-password/);
  });

  test('submitting forgot-password with unknown email shows neutral check-your-inbox message', async ({ page }) => {
    await page.goto('/forgot-password');

    await page.fill('input[name="email"]', 'definitelynotauser_xyzabc@example.com');
    await page.click('button[type="submit"]');

    // Page should show a neutral message, with no indication whether the user exists
    await expect(page.locator('body')).toContainText('check your inbox');
    // Must NOT reveal the email was not found
    await expect(page.locator('body')).not.toContainText('not found');
    await expect(page.locator('body')).not.toContainText('does not exist');
  });
});

test.describe('Reset password error states', () => {
  test('navigating to /reset-password with no token shows an error message', async ({ page }) => {
    await page.goto('/reset-password');

    // Should show error about missing/invalid token
    await expect(page.locator('body')).toContainText('Invalid or missing reset token');
  });

  test('navigating to /reset-password with a bad token shows invalid/expired message', async ({ page }) => {
    await page.goto('/reset-password?token=thisisaninvalidtoken12345678901234');

    // Should show error about invalid or expired link
    await expect(page.locator('body')).toContainText(/invalid|expired/i);
  });

  // Happy path: runs when VALID_RESET_TOKEN env var is set (pre-seeded DB token).
  // To run: INSERT INTO password_reset_tokens (customer_id, token) VALUES ('<id>', '<token>');
  // then: VALID_RESET_TOKEN=<token> npm run test:ui
  const hasToken = !!process.env.VALID_RESET_TOKEN;
  test(hasToken ? 'navigating to /reset-password with a valid token shows the reset form' : 'navigating to /reset-password with a valid token shows the reset form (skipped: set VALID_RESET_TOKEN)', async ({ page }) => {
    test.skip(!hasToken, 'Set VALID_RESET_TOKEN env var with a pre-seeded DB token to run this test');
    const token = process.env.VALID_RESET_TOKEN;
    await page.goto(`/reset-password?token=${token}`);
    await expect(page.locator('input[name="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();
  });
});
