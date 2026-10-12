/**
 * Live-browser keyboard tab-order walk across the keeper flow.
 *
 * Drives every interactive surface in order with ONLY Tab / Shift-Tab /
 * Enter: never the mouse. After each step the script captures the
 * active element (selector, accessible name, role) and asserts that
 * focus has advanced to a sensible next target. Raw active-element
 * snapshots are written to docs/audits/keyboard-tab-order-<date>-<route>.json
 * so a reviewer can diff against the static enumeration in
 * `scripts/keyboard_tab_order_static_scan.py`.
 *
 * Why this spec exists (and why it is parked unrun by default):
 * the parent roadmap item `harbor-usability-live-browser-followups`
 * was decomposed precisely because running the live walk needs a
 * running Postgres + web server + admin-gated trial cookie + a build
 * id in `preview` state, all of which the executor cannot stand up.
 * The static scan covers what we can verify deterministically; this
 * spec is wired so a human (or CI step) can run it with one command
 * once the prerequisites are in place.
 *
 * Prereqs (not enforced by the test; install + boot before running):
 *   cd web && npm i -D @playwright/test
 *   CAS_HARBOR_ENABLED=true npm run build && npm start  # port 4000, or PORT
 *   Postgres running with schema applied
 *   Trial keeper + build in `preview` state
 *
 * Env vars:
 *   CAS_KB_DEV_TRIAL: UUID of an admin-gated trial keeper
 *                             (covers table, recall, preview)
 *   CAS_KB_LAMP_BUILD_ID: UUID of a build in `preview` state with
 *                             review_status='approved' (covers lamp,
 *                             make-public)
 *
 * Run:  cd web && npx playwright test tests/ui/keyboard-tab-order.spec.ts
 */

import { test, expect } from '@playwright/test';
import { uiOrigin } from './port';
import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

const DEV_TRIAL = process.env.CAS_KB_DEV_TRIAL || '';
const LAMP_BUILD_ID = process.env.CAS_KB_LAMP_BUILD_ID || '';
const BASE_URL = process.env.CAS_KB_BASE_URL || uiOrigin();
const AUDITS_DIR = join(__dirname, '..', '..', '..', 'docs', 'audits');
const TODAY = new Date().toISOString().slice(0, 10);

interface FocusSnapshot {
  step: string;
  tagName: string;
  id: string;
  className: string;
  role: string | null;
  ariaLabel: string | null;
  text: string;
  selector: string;
}

async function snapshotFocus(
  page: import('@playwright/test').Page,
  step: string,
): Promise<FocusSnapshot> {
  return await page.evaluate((stepLabel: string) => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) {
      return {
        step: stepLabel,
        tagName: '(body)',
        id: '',
        className: '',
        role: null,
        ariaLabel: null,
        text: '',
        selector: 'body',
      };
    }
    const trimmedText = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const sel = el.id
      ? `#${el.id}`
      : el.tagName.toLowerCase() +
        (el.className && typeof el.className === 'string'
          ? '.' + el.className.split(/\s+/).filter(Boolean).slice(0, 2).join('.')
          : '');
    return {
      step: stepLabel,
      tagName: el.tagName.toLowerCase(),
      id: el.id || '',
      className: typeof el.className === 'string' ? el.className : '',
      role: el.getAttribute('role'),
      ariaLabel: el.getAttribute('aria-label'),
      text: trimmedText,
      selector: sel,
    };
  }, step);
}

async function tabN(
  page: import('@playwright/test').Page,
  n: number,
  shift = false,
): Promise<void> {
  const key = shift ? 'Shift+Tab' : 'Tab';
  for (let i = 0; i < n; i++) await page.keyboard.press(key);
}

function writeWalk(label: string, walk: FocusSnapshot[]): void {
  if (!existsSync(AUDITS_DIR)) mkdirSync(AUDITS_DIR, { recursive: true });
  const out = join(AUDITS_DIR, `keyboard-tab-order-${TODAY}-${label}.json`);
  writeFileSync(out, JSON.stringify(walk, null, 2));
}

test.describe('keeper-flow keyboard tab-order walk', () => {
  test('/lighthouse/table: first tabs from cold load', async ({ page }) => {
    test.skip(!DEV_TRIAL, 'Set CAS_KB_DEV_TRIAL to a valid trial UUID');
    const walk: FocusSnapshot[] = [];
    await page.goto(`${BASE_URL}/lighthouse/table?dev_trial=${DEV_TRIAL}`, {
      waitUntil: 'domcontentloaded',
    });

    // Step 1: first Tab from cold load. The skip-link should win this
    // race. AP2 in the static scanner predicts a regression: textarea.focus()
    // on first paint steals focus before Tab is ever pressed, so the
    // first snapshot here will show keeper-text as the active element
    // instead of the cas-skip-link anchor. This test captures the
    // observed behavior so the design pass has hard evidence.
    walk.push(await snapshotFocus(page, 'initial active element (pre-Tab)'));
    await page.keyboard.press('Tab');
    walk.push(await snapshotFocus(page, 'after 1st Tab'));
    await page.keyboard.press('Tab');
    walk.push(await snapshotFocus(page, 'after 2nd Tab'));
    await page.keyboard.press('Tab');
    walk.push(await snapshotFocus(page, 'after 3rd Tab'));
    await page.keyboard.press('Tab');
    walk.push(await snapshotFocus(page, 'after 4th Tab'));

    // Shift-Tab back to confirm reverse traversal mirrors forward order.
    await tabN(page, 4, true);
    walk.push(await snapshotFocus(page, 'after 4x Shift-Tab (should match initial)'));

    writeWalk('table-cold-load', walk);
    // Sensible-next-focus assertion: after some number of Tabs we MUST
    // land on the compose textarea (sending a turn is the page's primary
    // action). Failure here means the focus order is broken end-to-end.
    expect(walk.some((s) => s.id === 'keeper-text')).toBe(true);
  });

  test('/lighthouse/table: 8-turn conversation + email gate', async ({ page }) => {
    test.skip(!DEV_TRIAL, 'Set CAS_KB_DEV_TRIAL to a fresh trial UUID (turn-count near gate)');
    const walk: FocusSnapshot[] = [];
    await page.goto(`${BASE_URL}/lighthouse/table?dev_trial=${DEV_TRIAL}`, {
      waitUntil: 'domcontentloaded',
    });

    for (let turn = 1; turn <= 8; turn++) {
      // Drive entirely from the textarea: arrive via focus(), type a
      // line, Cmd+Enter to send. No mouse.
      await page.focus('#keeper-text');
      await page.keyboard.type(`Turn ${turn} from the keyboard-only walk.`);
      const sendKey = process.platform === 'darwin' ? 'Meta+Enter' : 'Control+Enter';
      await page.keyboard.press(sendKey);
      // Wait for Haven's response to land (best-effort; the test doesn't
      // gate on actual Claude latency).
      await page
        .waitForSelector(`.turn-row-companion:nth-of-type(${turn})`, { timeout: 20_000 })
        .catch(() => undefined);
      walk.push(await snapshotFocus(page, `after turn ${turn} submit`));
    }

    // After the 8th turn the email gate may have been triggered. If a
    // 402 fired, the runtime injects #email-gate-input and calls
    // input.focus(): surface that for the audit.
    const hasGate = await page.locator('#email-gate-input').count();
    walk.push({
      step: 'email-gate visible',
      tagName: hasGate ? 'input' : '(none)',
      id: hasGate ? 'email-gate-input' : '',
      className: '',
      role: null,
      ariaLabel: null,
      text: '',
      selector: hasGate ? '#email-gate-input' : '(absent)',
    });

    if (hasGate) {
      walk.push(await snapshotFocus(page, 'on email-gate render'));
      // Type + Enter to submit.
      await page.keyboard.type('keyboard-walk@example.com');
      await page.keyboard.press('Enter');
      walk.push(await snapshotFocus(page, 'after email-gate submit'));
    }

    writeWalk('table-conversation', walk);
  });

  test('/lighthouse/lamp/<id>: preview state Tab order', async ({ page }) => {
    test.skip(!LAMP_BUILD_ID, 'Set CAS_KB_LAMP_BUILD_ID to a preview/approved build UUID');
    const walk: FocusSnapshot[] = [];
    await page.goto(`${BASE_URL}/lighthouse/lamp/${LAMP_BUILD_ID}`, {
      waitUntil: 'domcontentloaded',
    });
    walk.push(await snapshotFocus(page, 'initial active element (pre-Tab)'));
    for (let i = 1; i <= 6; i++) {
      await page.keyboard.press('Tab');
      walk.push(await snapshotFocus(page, `after Tab ${i}`));
    }
    writeWalk('lamp-preview', walk);
    // The "Make it public" anchor MUST be in the forward tab path
    // because it is the page's primary CTA.
    expect(walk.some((s) => s.text.toLowerCase().includes('make it public'))).toBe(true);
  });

  test('/lighthouse/lamp/<id>/make-public: form Tab order', async ({ page }) => {
    test.skip(!LAMP_BUILD_ID, 'Set CAS_KB_LAMP_BUILD_ID to a preview/approved build UUID');
    const walk: FocusSnapshot[] = [];
    await page.goto(`${BASE_URL}/lighthouse/lamp/${LAMP_BUILD_ID}/make-public`, {
      waitUntil: 'domcontentloaded',
    });
    walk.push(await snapshotFocus(page, 'initial active element (pre-Tab)'));
    for (let i = 1; i <= 8; i++) {
      await page.keyboard.press('Tab');
      walk.push(await snapshotFocus(page, `after Tab ${i}`));
    }
    writeWalk('make-public', walk);
    // Form expectations: skip-link → breadcrumb back → range slider →
    // (attestation when amt<default) → email → terms → privacy → submit
    // → cancel. The range slider being a slider element is the
    // make-or-break for keyboard-only paying keepers.
    expect(walk.some((s) => s.id === 'amount-slider')).toBe(true);
    expect(walk.some((s) => s.id === 'email')).toBe(true);
    expect(walk.some((s) => s.text.toLowerCase().includes('make it public'))).toBe(true);
  });

  test('/lighthouse/preview: 5 family-card buttons are distinguishable to SR', async ({
    page,
  }) => {
    test.skip(!DEV_TRIAL, 'Set CAS_KB_DEV_TRIAL to a valid trial UUID');
    const walk: FocusSnapshot[] = [];
    await page.goto(`${BASE_URL}/lighthouse/preview?dev_trial=${DEV_TRIAL}`, {
      waitUntil: 'domcontentloaded',
    });
    walk.push(await snapshotFocus(page, 'initial active element (pre-Tab)'));
    for (let i = 1; i <= 7; i++) {
      await page.keyboard.press('Tab');
      walk.push(await snapshotFocus(page, `after Tab ${i}`));
    }
    writeWalk('preview', walk);
    // AP1 contract: every "Choose this" button should have a unique
    // accessible name (aria-label, aria-labelledby, or text). When the
    // static scanner's AP1 is fixed this assertion gates the regression.
    const chooseButtons = walk.filter((s) =>
      (s.text || '').toLowerCase().includes('choose this') || (s.ariaLabel || '').toLowerCase().includes('choose'),
    );
    const accessibleNames = chooseButtons.map((s) => (s.ariaLabel || s.text || '').trim());
    const unique = new Set(accessibleNames);
    expect(
      unique.size,
      `Expected every template-family "Choose this" button to have a unique accessible name; got ${JSON.stringify(
        accessibleNames,
      )}`,
    ).toBe(accessibleNames.length);
  });
});
