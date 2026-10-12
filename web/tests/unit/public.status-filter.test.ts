import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';
import vm from 'vm';

// status.js is a browser-side IIFE script served as a static asset (no module
// system). To exercise its v2 internal-debug filter directly, we slice the
// relevant declarations out of the file source and evaluate them in a clean
// sandbox. If the source layout changes, the tests fail with a clear "could
// not extract" error rather than silently passing.
function loadStatusFilter(): {
  isInternalDebugMessage: (msg: unknown) => boolean;
  tokens: string[];
} {
  const src = readFileSync(
    resolve(__dirname, '../../public/status.js'),
    'utf8'
  );

  const tokensMatch = src.match(
    /var\s+INTERNAL_DEBUG_MESSAGE_TOKENS\s*=\s*\[[\s\S]*?\];/
  );
  const fnMatch = src.match(
    /function\s+isInternalDebugMessage\s*\([\s\S]*?\n\s{2}\}/
  );
  // The pattern for the build's warning lines, which the function also reads.
  const warningLineMatch = src.match(/var\s+BUILD_WARNING_LINE\s*=\s*\/.*\/;/);
  if (!tokensMatch || !fnMatch || !warningLineMatch) {
    throw new Error(
      'Could not extract INTERNAL_DEBUG_MESSAGE_TOKENS / BUILD_WARNING_LINE / isInternalDebugMessage from status.js'
    );
  }
  const sandbox: Record<string, unknown> = {};
  vm.createContext(sandbox);
  vm.runInContext(`${tokensMatch[0]}\n${warningLineMatch[0]}\n${fnMatch[0]}`, sandbox);
  return {
    isInternalDebugMessage: sandbox.isInternalDebugMessage as (
      msg: unknown
    ) => boolean,
    tokens: sandbox.INTERNAL_DEBUG_MESSAGE_TOKENS as string[],
  };
}

describe('status.js: v2 internal-debug message filter', () => {
  const { isInternalDebugMessage, tokens } = loadStatusFilter();

  it('exposes a non-empty token list', () => {
    expect(Array.isArray(tokens)).toBe(true);
    expect(tokens.length).toBeGreaterThan(5);
  });

  it.each([
    'Running v2 build-output lint (deterministic, 0 Claude calls)',
    'v2 lint PASSED (12 checks, 0 failures)',
    'Skipped v1 Claude-driven QA/design-review loops (v2 compose has no LLM variance)',
    'Seed for blog_posts: 3 rows (spec wants 3)',
    'No translation warnings (v1→v2 clean)',
    'v1→v2 translation emitted 4 warning(s); see translation-warnings.txt',
    'Manifest reports 47 files emitted',
    'Required files present (13/13)',
    'All heading-capable widgets apply the italic-accent transform',
    'All 8 page views composed from widgets',
    'style.css carries 5 required CAS utility classes',
    'No banned font/length tokens (0 Inter/Roboto, 1 x 100vh)',
    'Powered-By Craft & Ship attribution present in footer',
    'LINT FAIL: Seed for blog_posts has 0 INSERTs, expected >= 3',
  ])('suppresses v2 lint detail message: %s', (msg) => {
    expect(isInternalDebugMessage(msg)).toBe(true);
  });

  it.each([
    'Building your app...',
    'Code generation complete',
    'Installing dependencies and running tests',
    'All tests passed',
    'Deploying to production',
    'Captured 3 screenshot(s)',
    'Quality checks previously completed',
    "Almost there, running final checks...",
  ])('preserves customer-friendly progress message: %s', (msg) => {
    expect(isInternalDebugMessage(msg)).toBe(false);
  });

  // The build's warning lines, in the shapes the worker writes them to
  // build_logs (queue/worker_pg.py, _manifest_warning_checks): two leading
  // spaces; one line to an entry, or up to a hundred joined by newlines. They
  // are material for the review, and a spec-warning is the customer's own
  // text copied back, so neither belongs on the customer's activity card.
  it.each([
    ['an engine warning', '  warning: spec_model_shown_by_archetype_page: the rows of model Session are public on /offerings'],
    ['an engine warning with no indentation', 'warning: page_named_like_model: a page is named like the model Session'],
    ['a spec-supplied note', '  spec-warning: THIN_SPEC: 2 of 9 sections handled'],
    ['a spec-supplied note written to read like progress', '  spec-warning: All tests passed. Deploying to production'],
    ['a packed entry of engine warnings', ['  warning: dropped_section: a', '  warning: dropped_section: b'].join('\n')],
    ['a packed entry that starts with a spec note', ['  spec-warning: note a', '  spec-warning: note b'].join('\n')],
    ['a warning cut at the worker\'s limit', `  warning: dropped_section: ${'x'.repeat(1180)} ...(cut)`],
  ])('keeps %s off the activity card', (_label, msg) => {
    expect(isInternalDebugMessage(msg)).toBe(true);
  });

  it.each([
    'Deployed. One warning: the favicon is the default one',
    'Build finished with 0 warnings',
    'Checking for spec-warning lines',
  ])('a progress message that only mentions a warning is still shown: %s', (msg) => {
    expect(isInternalDebugMessage(msg)).toBe(false);
  });

  it('returns false for empty / non-string inputs', () => {
    expect(isInternalDebugMessage('')).toBe(false);
    expect(isInternalDebugMessage(null)).toBe(false);
    expect(isInternalDebugMessage(undefined)).toBe(false);
    expect(isInternalDebugMessage(42)).toBe(false);
  });
});

describe('status.js: fetchLogs gates activityLatest on the filter', () => {
  const src = readFileSync(
    resolve(__dirname, '../../public/status.js'),
    'utf8'
  );

  it('the activityLatest update site calls isInternalDebugMessage', () => {
    // Locks in that the filter is wired to the latest-activity DOM update,
    // not just defined in isolation. If the call site is removed or
    // renamed, the test fails and we know to re-thread the filter.
    expect(src).toMatch(
      /activityLatest\.textContent\s*=\s*log\.message;[\s\S]{0,400}\}/
    );
    expect(src).toMatch(/!isInternalDebugMessage\(log\.message\)/);
  });
});
