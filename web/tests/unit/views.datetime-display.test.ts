/**
 * Regression guards for browser-local datetime display:
 *
 *  1. layout-harbor.ejs MUST carry the same data-ts upgrader script that
 *     layout.ejs has, so customer-facing Harbor pages can use the data-ts
 *     pattern. Without it, server-rendered UTC strings stay UTC in the
 *     viewer's browser, which is misleading for non-UTC customers.
 *
 *  2. status.ejs MUST wrap reviewed_at renderings in a data-ts span so the
 *     upgrader can rewrite them to the viewer's local timezone. Bare
 *     toLocaleDateString calls run on the server (Railway = UTC) and ignore
 *     the customer's locale.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const VIEWS_DIR = join(__dirname, '..', '..', 'views');
const HARBOR = readFileSync(join(VIEWS_DIR, 'layout-harbor.ejs'), 'utf-8');
const LEGACY = readFileSync(join(VIEWS_DIR, 'layout.ejs'), 'utf-8');
const STATUS = readFileSync(join(VIEWS_DIR, 'status.ejs'), 'utf-8');

describe('layout-harbor.ejs: data-ts upgrader script parity with layout.ejs', () => {
  it('layout-harbor.ejs runs a DOMContentLoaded handler that scans [data-ts] elements', () => {
    expect(HARBOR).toMatch(/document\.addEventListener\(['"]DOMContentLoaded['"]/);
    expect(HARBOR).toMatch(/document\.querySelectorAll\(['"]\[data-ts\]['"]\)/);
  });

  it('layout-harbor.ejs supports the date / datetime / time data-ts-fmt values', () => {
    expect(HARBOR).toContain("data-ts-fmt");
    expect(HARBOR).toContain("'datetime'");
    expect(HARBOR).toContain("'time'");
    expect(HARBOR).toMatch(/toLocaleDateString\(['"]en-US['"]/);
    expect(HARBOR).toMatch(/toLocaleString\(['"]en-US['"]/);
    expect(HARBOR).toMatch(/toLocaleTimeString\(['"]en-US['"]/);
  });

  it('layout-harbor.ejs datetime + time formats include timeZoneName for unambiguous TZ display', () => {
    const datetimeBlock = HARBOR.match(/fmt === ['"]datetime['"][\s\S]{0,400}/);
    const timeBlock = HARBOR.match(/fmt === ['"]time['"][\s\S]{0,400}/);
    expect(datetimeBlock).not.toBeNull();
    expect(timeBlock).not.toBeNull();
    expect(datetimeBlock![0]).toContain('timeZoneName');
    expect(timeBlock![0]).toContain('timeZoneName');
  });

  it('layout-harbor.ejs upgrader contract matches layout.ejs (same fmt values supported)', () => {
    // Both shells must support the same set of fmt keys so any data-ts span
    // in shared partials renders identically across legacy + harbor pages.
    for (const fmt of ['datetime', 'time']) {
      expect(HARBOR).toContain("'" + fmt + "'");
      expect(LEGACY).toContain("'" + fmt + "'");
    }
  });
});

describe('status.ejs: reviewed_at renders go through the data-ts pipeline', () => {
  it('every toLocaleDateString reviewed_at render is wrapped in a data-ts span', () => {
    // Find all toLocaleDateString call sites and confirm each is preceded
    // by a data-ts wrapper on the same line. Catches future regressions
    // where someone re-introduces a bare server-locale render.
    const lines = STATUS.split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!/toLocaleDateString/.test(line)) continue;
      // Each surviving call must be inside a data-ts span. Tolerate either
      // single or double quotes on the attribute.
      expect(line).toMatch(/data-ts=["']/);
      expect(line).toMatch(/data-ts-fmt=["']date["']/);
    }
  });

  it('status.ejs has at least two data-ts spans for reviewed_at (preview + live cards)', () => {
    const matches = STATUS.match(/data-ts=["']<%= [a-zA-Z]+\.toISOString\(\) %>["']/g) || [];
    expect(matches.length).toBeGreaterThanOrEqual(2);
  });
});
