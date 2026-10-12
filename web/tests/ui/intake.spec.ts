import { test, expect } from '@playwright/test';

// Haven-as-intake redirect contract: when CAS_HARBOR_ENABLED=true (set in
// the UI test workflow env), /intake and /try are 302'd to the canonical
// /lighthouse/table chat surface. These tests exercise the redirect and
// the basic shape of the lighthouse page so a regression in either the
// redirect or the page render is caught at the E2E layer.
//
// The lighthouse surface is cookie-identified (no login required); the
// trialCookie middleware mints a trial_keepers row on first visit. We do
// not test the AI response path here: it depends on Anthropic API
// availability and costs real money on every CI run, so the keeper-bubble
// assertion stops at the optimistic client-side append.

const testEmail = process.env.DEV_TEST_EMAIL;
const testPassword = process.env.DEV_TEST_PASSWORD;

test.describe('Haven-as-intake redirect (anonymous)', () => {
  test('GET /try redirects to /lighthouse/table', async ({ page }) => {
    const response = await page.goto('/try');
    expect(response?.status()).toBe(200); // final response after redirect chain
    await expect(page).toHaveURL('/lighthouse/table');
  });

  test('/lighthouse/table renders Haven header + textarea + opening turn', async ({ page }) => {
    await page.goto('/lighthouse/table');

    // Haven's header (avatar + name + subtitle) is the first thing the
    // keeper sees inside the keepers-table container.
    await expect(page.locator('.keepers-table-header')).toBeVisible();
    await expect(page.locator('.haven-header-avatar')).toBeVisible();

    // Opening companion turn renders without any AI call (deterministic
    // text from openingMessage() in companion/runtime.ts).
    const opening = page.locator('.turn-companion').first();
    await expect(opening).toBeVisible();
    await expect(opening).toContainText(/glad you found your way here/i);

    // Compose textarea + Send button are present and ready to type into.
    // NOTE: on a COLD load (no same-origin referrer) the textarea is
    // deliberately NOT autofocused: focus is left on the skip-to-content
    // link so keyboard users land there first (WCAG 2.4.1 / 2.4.3 / 3.2.1;
    // see table.ejs initial-state block + docs/audits/keyboard-tab-order-
    // 2026-05-27.md AP2). So we assert the textarea is editable/ready, not
    // focused. The same-origin-referrer autofocus path is covered below.
    const textarea = page.locator('textarea[name="keeperText"]');
    await expect(textarea).toBeVisible();
    await expect(textarea).toBeEditable();
    await expect(textarea).not.toBeFocused();
    await expect(page.locator('#send-btn')).toBeVisible();
  });

  test('autofocuses the textarea when returning from a same-origin lighthouse surface', async ({ page }) => {
    // The initial-state block in table.ejs focuses the compose textarea only
    // when document.referrer is same-origin (the keeper is coming back from
    // /recall, /preview, or /lamp), so the skip-link still wins on cold load
    // but a returning keeper lands ready to type. Simulate that by first
    // visiting a same-origin lighthouse page, then navigating to the table
    // via an in-page link so the referrer is set.
    await page.goto('/lighthouse/table/recall');
    // Navigate to the table with a same-origin referrer by setting it on a
    // client-side navigation. A direct page.goto resets the referrer, so we
    // drive the navigation from within the already-loaded same-origin page.
    await page.evaluate(() => {
      window.location.href = '/lighthouse/table';
    });
    await expect(page).toHaveURL('/lighthouse/table');

    const textarea = page.locator('textarea[name="keeperText"]');
    await expect(textarea).toBeVisible();
    await expect(textarea).toBeFocused();
  });

  test('Enter alone adds a newline (letter register; submit is Cmd+Enter)', async ({ page }) => {
    await page.goto('/lighthouse/table');

    const textarea = page.locator('textarea[name="keeperText"]');
    await textarea.focus();
    await textarea.fill('First line');
    await textarea.press('Enter');
    await textarea.type('Second line');

    const value = await textarea.inputValue();
    expect(value).toContain('\n');

    // No keeper bubble was sent; only the opening companion turn exists.
    await expect(page.locator('.turn-keeper')).toHaveCount(0);
  });

  test('Cmd+Enter optimistically appends a keeper bubble', async ({ page }) => {
    await page.goto('/lighthouse/table');

    const textarea = page.locator('textarea[name="keeperText"]');
    await textarea.fill('I want to build a booking site for my hair salon');

    // appendKeeperTurn() fires synchronously on form submit before the
    // network round-trip, so the keeper bubble shows up regardless of
    // whether the Anthropic API call succeeds. Asserting only this side
    // of the exchange keeps the test deterministic + cheap.
    await textarea.press('ControlOrMeta+Enter');

    const keeperBubble = page.locator('.turn-keeper').last();
    await expect(keeperBubble).toBeVisible({ timeout: 5000 });
    await expect(keeperBubble).toContainText('booking site for my hair salon');
  });
});

test.describe('Haven-as-intake redirect (authenticated)', () => {
  test.skip(!testEmail || !testPassword, 'Set DEV_TEST_EMAIL and DEV_TEST_PASSWORD to run authed redirect tests');

  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="email"]', testEmail!);
    await page.fill('input[name="password"]', testPassword!);
    await page.click('button[type="submit"]');
    await expect(page).toHaveURL('/dashboard');
  });

  test('GET /intake redirects to /lighthouse/table for logged-in users', async ({ page }) => {
    await page.goto('/intake');
    await expect(page).toHaveURL('/lighthouse/table');
    await expect(page.locator('.keepers-table-header')).toBeVisible();
  });
});
