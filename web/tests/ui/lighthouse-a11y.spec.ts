/**
 * Live-browser accessibility sweep across /lighthouse/* surfaces.
 *
 * Catches the failure modes the static pairing scanner at
 * `scripts/lighthouse_a11y_contrast_scan.py` cannot see: dynamic state,
 * overlapping elements, focus rings, hover states, ARIA roles, name/role
 * for interactive controls, contrast against actual composited backgrounds
 * (gradients, paper-grain radial layers), and skip-link behavior.
 *
 * Prereqs (not enforced by the test; install + boot before running):
 *   cd web && npm i -D @axe-core/playwright axe-core
 *   CAS_HARBOR_ENABLED=true npm run build && npm start  # port 4000, or PORT
 *   Postgres running with schema applied
 *   A trial keeper cookie + a build id (set via env vars below)
 *
 * Env vars:
 *   CAS_A11Y_LAMP_BUILD_ID: UUID of a build in `preview` state (for lamp routes)
 *   CAS_A11Y_DEV_TRIAL: UUID of a trial keeper (admin-gated dev_trial override)
 *
 * Run:  cd web && npx playwright test tests/ui/lighthouse-a11y.spec.ts
 *
 * Output:
 *   docs/audits/lighthouse-a11y-<date>.json (machine-readable raw axe output)
 *   Markdown digest appended to docs/audits/lighthouse-a11y-<date>.md by hand.
 */

import { test, expect } from '@playwright/test';
import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

const LAMP_BUILD_ID = process.env.CAS_A11Y_LAMP_BUILD_ID || '';
const DEV_TRIAL = process.env.CAS_A11Y_DEV_TRIAL || '';
// Build (lamp) routes are owned by an authed keeper (resolveLampOwnership
// Path 1). The fixture build CAS_A11Y_LAMP_BUILD_ID must belong to this
// customer; we log in as them before scanning build routes. Trial routes
// (table/recall/preview) need no login: the trialCookie middleware mints
// a cookie identity on first visit.
const KEEPER_EMAIL = process.env.DEV_TEST_EMAIL || '';
const KEEPER_PASSWORD = process.env.DEV_TEST_PASSWORD || '';
const AUDITS_DIR = join(__dirname, '..', '..', '..', 'docs', 'audits');
const TODAY = new Date().toISOString().slice(0, 10);

interface Route {
  path: string;
  needs: 'trial' | 'build' | 'none';
  label: string;
}

const ROUTES: Route[] = [
  { path: '/lighthouse/table', needs: 'trial', label: 'table' },
  { path: '/lighthouse/table/recall', needs: 'trial', label: 'recall' },
  { path: '/lighthouse/preview', needs: 'trial', label: 'preview' },
  { path: `/lighthouse/lamp/${LAMP_BUILD_ID}`, needs: 'build', label: 'lamp' },
  { path: `/lighthouse/lamp/${LAMP_BUILD_ID}/make-public`, needs: 'build', label: 'make-public' },
  { path: `/lighthouse/lamp/${LAMP_BUILD_ID}/published`, needs: 'build', label: 'published' },
  { path: `/lighthouse/lamp/${LAMP_BUILD_ID}/cancelled`, needs: 'build', label: 'cancelled' },
];

interface AxeNode {
  html: string;
  target: string[];
  failureSummary?: string;
  any?: Array<{ id: string; message: string; data?: unknown }>;
}

interface AxeViolation {
  id: string;
  impact: string | null;
  description: string;
  help: string;
  helpUrl: string;
  nodes: AxeNode[];
}

interface AxeResults {
  violations: AxeViolation[];
  passes: Array<{ id: string }>;
  url: string;
  timestamp: string;
}

// Resolve the optional axe dependency once at module load. When it is absent
// (e.g. a minimal install), every scan below skips cleanly with a clear
// reason rather than crashing the whole describe. Do NOT use expect.fail()
// here: it is a Vitest/Chai API, not Playwright, and calling it throws
// "expect.fail is not a function", which is exactly the breakage this guard
// is meant to prevent.
const AXE_AVAILABLE: boolean = (() => {
  try {
    require.resolve('@axe-core/playwright');
    return true;
  } catch {
    return false;
  }
})();

async function runAxe(page: import('@playwright/test').Page): Promise<AxeResults> {
  const { default: AxeBuilder } = require('@axe-core/playwright');
  const builder = new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .disableRules(['region']); // permits the chat-only surface w/o landmark wrapping
  const results = await builder.analyze();
  return results as AxeResults;
}

function writeArtifact(label: string, results: AxeResults): void {
  if (!existsSync(AUDITS_DIR)) mkdirSync(AUDITS_DIR, { recursive: true });
  const out = join(AUDITS_DIR, `lighthouse-a11y-${TODAY}-${label}.json`);
  writeFileSync(out, JSON.stringify(results, null, 2));
}

test.describe('/lighthouse/* axe sweep', () => {
  for (const route of ROUTES) {
    const missingFixture =
      (route.needs === 'trial' && !DEV_TRIAL) ||
      (route.needs === 'build' && (!LAMP_BUILD_ID || !KEEPER_EMAIL || !KEEPER_PASSWORD));

    test(`${route.label}: axe scan`, async ({ page }) => {
      test.skip(!AXE_AVAILABLE, 'Install @axe-core/playwright + axe-core to run the a11y sweep');
      test.skip(missingFixture, `Missing fixture (${route.needs}): set CAS_A11Y_DEV_TRIAL / CAS_A11Y_LAMP_BUILD_ID (+ DEV_TEST_EMAIL/PASSWORD for build routes)`);

      // Build routes need an authed keeper session that owns the fixture build.
      if (route.needs === 'build') {
        await page.goto('/login');
        await page.fill('input[name="email"]', KEEPER_EMAIL);
        await page.fill('input[name="password"]', KEEPER_PASSWORD);
        await page.click('button[type="submit"]');
        await expect(page).toHaveURL('/dashboard');
      }

      const url = route.needs === 'trial'
        ? `${route.path}?dev_trial=${DEV_TRIAL}`
        : route.path;
      const resp = await page.goto(url, { waitUntil: 'domcontentloaded' });
      expect(resp?.status(), `expected 2xx for ${route.path}`).toBeLessThan(400);

      const results = await runAxe(page);
      writeArtifact(route.label, results);

      const contrastViolations = results.violations.filter(
        (v) => v.id === 'color-contrast' || v.id === 'color-contrast-enhanced',
      );

      // Contrast is the focus of this sub-item, but log everything for visibility.
      if (results.violations.length > 0) {
        console.log(
          `[a11y/${route.label}] ${results.violations.length} violation(s); ` +
            `${contrastViolations.length} contrast`,
        );
      }

      expect(
        contrastViolations,
        `Contrast violations on ${route.path}; see ` +
          `docs/audits/lighthouse-a11y-${TODAY}-${route.label}.json`,
      ).toEqual([]);
    });
  }
});
