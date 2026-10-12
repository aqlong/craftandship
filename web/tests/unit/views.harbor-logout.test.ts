/**
 * Regression guard: harbor + legacy layout Sign out must be a CSRF-protected
 * POST form, never a bare <a href="/logout"> link.
 *
 * Background: the /logout route is POST-only with CSRF validation. A bare
 * <a> tag triggers a GET request which 404s, leaving logged-in customers
 * unable to log out. This test fails fast if the form is reverted to a link.
 *
 * Companion full-flow check: web/tests/ui/auth.spec.ts:93 ('logout redirects
 * to home and shows Log In link'): runs in CI when CAS_HARBOR_ENABLED=true.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const VIEWS_DIR = join(__dirname, '..', '..', 'views');
const HARBOR = readFileSync(join(VIEWS_DIR, 'layout-harbor.ejs'), 'utf-8');
const LEGACY = readFileSync(join(VIEWS_DIR, 'layout.ejs'), 'utf-8');

describe('harbor + legacy layouts: Sign out is a CSRF-protected POST form', () => {
  it('layout-harbor.ejs has NO bare <a href="/logout"> anchors', () => {
    // Bare GET link to a POST-only route is the historic bug. Match either single
    // or double quotes; tolerate any attributes between <a and href.
    const bareLinkPattern = /<a\b[^>]*\bhref=["']\/logout["']/i;
    expect(HARBOR).not.toMatch(bareLinkPattern);
  });

  it('layout.ejs has NO bare <a href="/logout"> anchors', () => {
    const bareLinkPattern = /<a\b[^>]*\bhref=["']\/logout["']/i;
    expect(LEGACY).not.toMatch(bareLinkPattern);
  });

  it('layout-harbor.ejs nav has a POST form to /logout with CSRF + Sign out button', () => {
    // Find the (logged-in) nav block that includes the dashboard link
    expect(HARBOR).toContain('action="/logout"');
    expect(HARBOR).toContain('method="POST"');
    // CSRF field must be included via the partial
    const formMatches = HARBOR.match(/<form action="\/logout"[\s\S]*?<\/form>/g) || [];
    expect(formMatches.length).toBeGreaterThanOrEqual(2); // top nav + footer
    for (const form of formMatches) {
      expect(form).toContain("include('_csrf-field')");
      expect(form).toMatch(/<button[^>]*type="submit"[^>]*>Sign out<\/button>/);
    }
  });

  it('layout.ejs nav has a POST form to /logout with CSRF + Sign out button', () => {
    expect(LEGACY).toContain('action="/logout"');
    expect(LEGACY).toContain('method="POST"');
    const formMatches = LEGACY.match(/<form action="\/logout"[\s\S]*?<\/form>/g) || [];
    expect(formMatches.length).toBeGreaterThanOrEqual(1);
    for (const form of formMatches) {
      expect(form).toContain("include('_csrf-field')");
      expect(form).toMatch(/<button[^>]*type="submit"[^>]*>Sign out<\/button>/);
    }
  });

  it('layout-harbor.ejs nav uses "My websites" (plural)', () => {
    // Multiple customers can have multiple builds, so the nav link is plural.
    // The dashboard <h1> is conditionally pluralized separately in dashboard.ejs.
    expect(HARBOR).toMatch(/>My websites</);
    expect(HARBOR).not.toMatch(/>My website</);
  });

  it('layout.ejs nav uses "My websites" (plural)', () => {
    expect(LEGACY).toMatch(/>My websites</);
    expect(LEGACY).not.toMatch(/>My website</);
  });
});

describe('layout-harbor.ejs: responsive hamburger nav guarantees no bleed', () => {
  it('has a .nav-toggle button with proper aria attributes', () => {
    expect(HARBOR).toMatch(/<button[^>]*\bclass="[^"]*\bnav-toggle\b/);
    expect(HARBOR).toMatch(/aria-controls="nav-menu"/);
    expect(HARBOR).toMatch(/aria-expanded="false"/);
    expect(HARBOR).toMatch(/aria-label="Open menu"/);
  });

  it('the nav-links container has id="nav-menu" so the toggle can target it', () => {
    expect(HARBOR).toMatch(/<nav\s+id="nav-menu"[^>]*\bclass="[^"]*\bnav-links\b/);
  });

  it('the standalone .nav-cta (btn-primary) is a sibling of .nav-links so it shows at wider widths', () => {
    // Pull out the nav-inner block and assert the standalone CTA appears OUTSIDE the nav-links <nav>
    const navInnerMatch = HARBOR.match(/<div[^>]*\bclass="max-site nav-inner"[\s\S]*?<\/div>\s*<\/header>/);
    expect(navInnerMatch).toBeTruthy();
    const navInner = navInnerMatch![0];
    // Find where the nav-links <nav> ends and where the standalone CTA starts. Match the
    // btn-primary CTA specifically so the nested .nav-cta-mobile link is not picked up.
    const navLinksEndIdx = navInner.indexOf('</nav>');
    const ctaIdx = navInner.search(/<a[^>]*\bclass="btn btn-primary nav-cta"/);
    expect(navLinksEndIdx).toBeGreaterThan(0);
    expect(ctaIdx).toBeGreaterThan(navLinksEndIdx);
  });

  it('a .nav-cta-mobile begin link lives INSIDE the dropdown for narrow widths where the bar CTA is hidden', () => {
    // The standalone CTA is hidden below 560px; this nested link keeps the conversion
    // path reachable from inside the open menu.
    const navInnerMatch = HARBOR.match(/<div[^>]*\bclass="max-site nav-inner"[\s\S]*?<\/div>\s*<\/header>/);
    const navInner = navInnerMatch![0];
    const navMenu = navInner.match(/<nav\s+id="nav-menu"[\s\S]*?<\/nav>/)![0];
    expect(navMenu).toMatch(/\bclass="nav-cta-mobile"/);
    expect(navMenu).toMatch(/>Begin a conversation<\/a>/);
    // CSS contract: hidden by default, shown (and the bar CTA hidden) at <=560px.
    expect(HARBOR).toMatch(/\.harbor\s+\.nav-cta-mobile\s*\{\s*display:\s*none/);
    expect(HARBOR).toMatch(/@media\s*\(max-width:\s*560px\)[\s\S]*?\.harbor\s+\.nav-cta\s*\{\s*display:\s*none/);
  });

  it('inline JS toggles the nav menu open + closes on outside-click + Escape', () => {
    // The hamburger toggle relies on a small inline script. Assert key behaviors.
    expect(HARBOR).toMatch(/classList\.add\(['"]is-open['"]\)/);
    expect(HARBOR).toMatch(/classList\.remove\(['"]is-open['"]\)/);
    expect(HARBOR).toMatch(/['"]Escape['"]/);
    expect(HARBOR).toMatch(/aria-expanded/);
  });

  it('CSS includes a max-width:1280px breakpoint that flips nav-links to a dropdown panel', () => {
    expect(HARBOR).toMatch(/@media\s*\(\s*max-width:\s*1280px\s*\)/);
    // The dropdown rule sets position: absolute on .nav-links so it floats below the toggle
    expect(HARBOR).toMatch(/\.nav-links\s*\{[\s\S]*?position:\s*absolute/);
  });
});
