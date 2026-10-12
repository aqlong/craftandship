/**
 * Live-browser screen-reader walk of the /lighthouse/table conversation
 * surface.
 *
 * Sub-3 of the parent roadmap item `harbor-usability-live-browser-followups`
 * (decomposed 2026-05-27). This spec captures the contract that a screen
 * reader user gets a usable conversation experience on `/lighthouse/table`:
 *
 *   SR1, When the keeper submits a turn and Haven's reply appends to the
 *         conversation, the appended companion turn (or its containing
 *         conversation log) carries an `aria-live` region so the screen
 *         reader announces it. Without this, an SR user has no way to
 *         know Haven has replied. WCAG 4.1.3 Status Messages.
 *
 *   SR2: The compose textarea (#keeper-text) has an associated label
 *         (`<label for="keeper-text">` OR `aria-label` on the input).
 *         WCAG 1.3.1 Info and Relationships + 3.3.2 Labels or Instructions.
 *
 *   SR3: The send button (#send-btn) has an accessible name. Either an
 *         explicit `aria-label` attribute or non-empty visible text that
 *         the ARIA name-computation algorithm picks up. WCAG 4.1.2 Name,
 *         Role, Value.
 *
 *   SR4: The eyebrow text in the conversation header ("your companion")
 *         is accessible-name'd alongside the companion name
 *         ("Haven"). Either the header element carries `aria-label` /
 *         `aria-labelledby` binding both, or the eyebrow is a plain text
 *         node a screen reader will read in order with the name. The
 *         intent: a screen-reader user landing on the page hears "Haven,
 *         your companion" together, not "Haven" alone.
 *
 * Why this spec exists (and why it is parked unrun by default):
 *
 * The parent task `harbor-usability-live-browser-followups` was decomposed
 * because running a live walk needs the same infra as Sub-1 (axe sweep)
 * and Sub-2 (keyboard tab-order): a running web server, a Postgres with
 * schema applied, an admin-gated trial cookie, and `CAS_HARBOR_ENABLED=true`.
 * The executor cannot stand any of those up, so the spec is wired and
 * parked, and the static findings are recorded in
 * `docs/audits/screen-reader-conversation-2026-05-27.md` for the design
 * pass.
 *
 * Prereqs (not enforced by the test; install + boot before running):
 *   cd web && npm i -D @playwright/test
 *   CAS_HARBOR_ENABLED=true npm run build && npm start  # port 4000, or PORT
 *   Postgres running with schema applied
 *   A trial keeper cookie (admin-gated dev_trial override)
 *
 * Env vars:
 *   CAS_SR_DEV_TRIAL: UUID of an admin-gated trial keeper. Use a fresh
 *                        trial whose turn-count is well below the email
 *                        gate so the turn-submission step does not 402.
 *   CAS_SR_BASE_URL: optional override (defaults to http://localhost:$PORT, port 4000 when PORT is unset)
 *
 * Run:  cd web && npx playwright test tests/ui/screen-reader-conversation.spec.ts
 *
 * Output:
 *   docs/audits/screen-reader-conversation-<date>-table.json (raw walk)
 *   The hand-curated audit lives at
 *   docs/audits/screen-reader-conversation-2026-05-27.md.
 */

import { test, expect } from '@playwright/test';
import { uiOrigin } from './port';
import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

const DEV_TRIAL = process.env.CAS_SR_DEV_TRIAL || '';
const BASE_URL = process.env.CAS_SR_BASE_URL || uiOrigin();
const AUDITS_DIR = join(__dirname, '..', '..', '..', 'docs', 'audits');
const TODAY = new Date().toISOString().slice(0, 10);

interface AccessibleNode {
  role: string | null;
  name: string;
  ariaLive: string | null;
  hasLabelFor: boolean;
  ariaLabel: string | null;
  ariaLabelledBy: string | null;
  textContent: string;
  tagName: string;
  id: string;
}

interface WalkArtifact {
  url: string;
  capturedAt: string;
  conversationContainer: AccessibleNode | null;
  appendedCompanionTurn: AccessibleNode | null;
  composeTextarea: AccessibleNode | null;
  sendButton: AccessibleNode | null;
  headerName: AccessibleNode | null;
  headerEyebrow: AccessibleNode | null;
  headerElement: AccessibleNode | null;
  pulseWriting: AccessibleNode | null;
  emailGateInput: AccessibleNode | null;
}

function writeArtifact(label: string, walk: WalkArtifact): void {
  if (!existsSync(AUDITS_DIR)) mkdirSync(AUDITS_DIR, { recursive: true });
  const out = join(
    AUDITS_DIR,
    `screen-reader-conversation-${TODAY}-${label}.json`,
  );
  writeFileSync(out, JSON.stringify(walk, null, 2));
}

async function snapshotNode(
  page: import('@playwright/test').Page,
  selector: string,
): Promise<AccessibleNode | null> {
  return await page.evaluate((sel: string) => {
    const el = document.querySelector(sel) as HTMLElement | null;
    if (!el) return null;
    const labelForRef = el.id
      ? document.querySelector(`label[for="${el.id}"]`)
      : null;
    return {
      role: el.getAttribute('role'),
      name: el.getAttribute('aria-label') || (el.textContent || '').trim().slice(0, 120),
      ariaLive: el.getAttribute('aria-live'),
      hasLabelFor: !!labelForRef,
      ariaLabel: el.getAttribute('aria-label'),
      ariaLabelledBy: el.getAttribute('aria-labelledby'),
      textContent: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200),
      tagName: el.tagName.toLowerCase(),
      id: el.id || '',
    };
  }, selector);
}

/**
 * Returns true if `node` is in a screen-reader-announced region. A node
 * is announced when itself OR any ancestor has aria-live set to a value
 * other than "off", OR role="log" / role="status" / role="alert" (each
 * carries an implicit aria-live).
 */
async function isInLiveRegion(
  page: import('@playwright/test').Page,
  selector: string,
): Promise<boolean> {
  return await page.evaluate((sel: string) => {
    let el: HTMLElement | null = document.querySelector(sel) as HTMLElement | null;
    while (el) {
      const live = el.getAttribute('aria-live');
      if (live && live !== 'off') return true;
      const role = el.getAttribute('role');
      if (role && ['log', 'status', 'alert'].includes(role)) return true;
      el = el.parentElement;
    }
    return false;
  }, selector);
}

test.describe('/lighthouse/table: screen-reader conversation walk', () => {
  test('appended companion turn lands in an aria-live region', async ({ page }) => {
    test.skip(!DEV_TRIAL, 'Set CAS_SR_DEV_TRIAL to a fresh trial UUID');

    await page.goto(`${BASE_URL}/lighthouse/table?dev_trial=${DEV_TRIAL}`, {
      waitUntil: 'domcontentloaded',
    });

    // Snapshot the conversation container BEFORE submitting a turn so
    // the audit JSON records the aria-live state on cold load (not just
    // after JS has mutated the DOM).
    const conversationContainer = await snapshotNode(page, '#conversation');
    const headerElement = await snapshotNode(page, '.keepers-table-header');
    const headerName = await snapshotNode(page, '.keepers-table-header .name');
    const headerEyebrow = await snapshotNode(
      page,
      '.keepers-table-header .subtitle',
    );
    const composeTextarea = await snapshotNode(page, '#keeper-text');
    const sendButton = await snapshotNode(page, '#send-btn');

    // Submit a turn from the keyboard so we observe the same code path
    // a real keeper hits. Type + Cmd+Enter (or Ctrl+Enter on Linux).
    await page.focus('#keeper-text');
    await page.keyboard.type('Hello from the screen-reader contract walk.');
    const sendKey = process.platform === 'darwin' ? 'Meta+Enter' : 'Control+Enter';
    await page.keyboard.press(sendKey);

    // Pulse-writing affordance is the optimistic "Haven is writing." line
    // appended to the conversation. SR contract: if it shows at all, it
    // must live in a live region so the SR user knows Haven is working.
    const pulseWriting = await snapshotNode(page, '#pulse-writing');

    // Wait for Haven's reply to land. Selector targets the LAST
    // companion turn row, which is appended via appendCompanionTurn().
    await page
      .waitForSelector('.turn-row-companion:last-of-type', { timeout: 30_000 })
      .catch(() => undefined);

    const appendedCompanionTurn = await snapshotNode(
      page,
      '.turn-row-companion:last-of-type .turn-companion',
    );

    // Email-gate may or may not have fired. Surface either way.
    const emailGateInput = await snapshotNode(page, '#email-gate-input');

    const walk: WalkArtifact = {
      url: page.url(),
      capturedAt: new Date().toISOString(),
      conversationContainer,
      appendedCompanionTurn,
      composeTextarea,
      sendButton,
      headerName,
      headerEyebrow,
      headerElement,
      pulseWriting,
      emailGateInput,
    };
    writeArtifact('table', walk);

    // ---- SR1: appended companion turn must be in a live region.
    // Either the conversation container carries aria-live (or role=log /
    // role=status), OR the appended turn itself does. Either satisfies
    // WCAG 4.1.3. The container path is preferred because it covers
    // every future turn without per-turn attribute plumbing.
    const conversationAnnounces = await isInLiveRegion(page, '#conversation');
    const turnAnnounces = await isInLiveRegion(
      page,
      '.turn-row-companion:last-of-type .turn-companion',
    );
    expect(
      conversationAnnounces || turnAnnounces,
      'Neither the conversation container (#conversation) nor the ' +
        'appended companion turn (.turn-row-companion:last-of-type .turn-companion) ' +
        'is inside an aria-live region (or role=log / role=status / role=alert). ' +
        'Screen-reader users will not be notified when Haven replies. See ' +
        `docs/audits/screen-reader-conversation-${TODAY}-table.json for the ` +
        'captured state.',
    ).toBe(true);

    // ---- SR2: compose textarea has an associated label.
    // <label for="keeper-text"> binds via the `for` attribute; an
    // aria-label or aria-labelledby on the textarea also satisfies the
    // contract. Multiple binding mechanisms are fine; at least one is
    // required.
    expect(
      composeTextarea,
      'Compose textarea #keeper-text not found on the page.',
    ).not.toBeNull();
    const textareaHasLabel = !!(
      composeTextarea?.hasLabelFor ||
      composeTextarea?.ariaLabel ||
      composeTextarea?.ariaLabelledBy
    );
    expect(
      textareaHasLabel,
      'Compose textarea #keeper-text has no associated <label for>, ' +
        'aria-label, or aria-labelledby. Screen readers will announce ' +
        'the textarea as an unlabeled "edit text" field. WCAG 1.3.1 + 3.3.2.',
    ).toBe(true);

    // ---- SR3: send button has an accessible name.
    // Either aria-label or non-empty visible text content satisfies the
    // ARIA name-computation algorithm for <button>. The current view
    // ships visible text "Send" with no aria-label; both forms pass.
    expect(sendButton, 'Send button #send-btn not found on the page.').not.toBeNull();
    const sendHasName = !!(
      (sendButton?.ariaLabel && sendButton.ariaLabel.trim().length > 0) ||
      (sendButton?.textContent && sendButton.textContent.trim().length > 0)
    );
    expect(
      sendHasName,
      'Send button #send-btn has neither aria-label nor visible text ' +
        'content; screen readers will announce it as an unnamed "button". ' +
        'WCAG 4.1.2.',
    ).toBe(true);

    // ---- SR4: visible eyebrow text is accessible-name'd.
    // The header pairs the companion name ("Haven") with an italic
    // subtitle ("your companion"). The contract: a screen-reader
    // user lands on the page and hears BOTH together; either because
    // the header carries an aria-label binding both, or because both
    // text nodes live in document order in the accessibility tree.
    // The latter (plain text in order) is the cheap default for
    // <span> children of a <header> element. This assertion sticks to
    // the visible-text-readable contract.
    expect(headerName, 'Header name span (.name) not found.').not.toBeNull();
    expect(
      headerEyebrow,
      'Header eyebrow span (.subtitle) not found; the "your ' +
        'companion" copy must remain a discoverable text node.',
    ).not.toBeNull();
    const eyebrowText = (headerEyebrow?.textContent || '').toLowerCase();
    expect(
      eyebrowText.includes('your companion'),
      `Eyebrow text content was "${headerEyebrow?.textContent}"; expected ` +
        '"your companion". If the copy intentionally changed, ' +
        'update this assertion and the audit doc.',
    ).toBe(true);

    // The header element itself does not need an aria-label as long as
    // the contained text is reachable in the AT tree. Capture aria-label
    // state for the audit either way.
    expect(headerElement, 'Header element .keepers-table-header not found.').not.toBeNull();
  });
});
