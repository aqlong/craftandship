#!/usr/bin/env node
/* CAS v2 post-build responsive lint (v2-build-responsive-lints).
 *
 * Renders pages headless and audits the COMPUTED DOM, not the source
 * (source-reading can say everything is fine while only computed
 * geometry exposes 250-360px dead bands and a 768-1150px nav
 * overflow). Checks, per viewport (360 / 768 / 1100 / 1280):
 *
 *   1. dead-band: any inter-content vertical gap > 220px at desktop
 *      widths (the blanket section-padding bug class).
 *   2. overflow: any element wider than the viewport, INCLUDING ones
 *      masked by overflow:hidden (no scrollbar betrays it).
 *   3. nav-fit: the horizontal nav must not wrap to a second line at
 *      widths where the hamburger is hidden (the editorial
 *      nav broke between 768 and 1150px).
 *
 * Usage:
 *   node scripts/responsive_lint.mjs <base_url> [path ...]
 *   node scripts/responsive_lint.mjs https://app.up.railway.app / /books /contact
 *
 * Paths default to "/" when omitted. Exits 1 when any check fails, so a
 * caller can gate on it. Requires playwright (available in web/node_modules).
 */
import { createRequire } from 'module';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(__dirname, '..', 'web', 'package.json'));
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (e) {
  console.error('playwright not available (expected in web/node_modules):', e.message);
  process.exit(2);
}

const VIEWPORTS = [
  { width: 360, height: 740, desktop: false },
  { width: 768, height: 900, desktop: false },
  { width: 1100, height: 800, desktop: true },
  { width: 1280, height: 800, desktop: true },
];
const DEAD_BAND_PX = 220;

const [baseUrl, ...rawPaths] = process.argv.slice(2);
if (!baseUrl) {
  console.error('usage: node scripts/responsive_lint.mjs <base_url> [path ...]');
  process.exit(2);
}
const paths = rawPaths.length > 0 ? rawPaths : ['/'];

const AUDIT_FN = ({ deadBandPx, isDesktop }) => {
  const findings = [];
  const vw = window.innerWidth;

  // --- overflow: any element wider than the viewport (even masked).
  // Page-level scrollWidth gates the partial-offscreen case so
  // off-canvas drawers (translated off-screen by design, body
  // overflow-x hidden) don't false-positive; genuinely oversized
  // boxes (width > vw) are flagged unconditionally because they clip
  // behind overflow:hidden with no scrollbar.
  const pageOverflows = document.documentElement.scrollWidth > vw + 1;
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.width > vw + 1 || (pageOverflows && r.right > vw + 1 && r.width > 40)) {
      const tag = el.tagName.toLowerCase();
      const cls = (el.className && String(el.className).slice(0, 80)) || '';
      findings.push({
        check: 'overflow',
        detail: `<${tag} class="${cls}"> width=${Math.round(r.width)} right=${Math.round(r.right)} vw=${vw}`,
      });
      if (findings.length > 20) break;
    }
  }

  // --- dead bands: collect content boxes (text/img/input), sort by top,
  // flag vertical gaps > deadBandPx (desktop only; mobile stacks tall).
  if (isDesktop) {
    const boxes = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
      const el = walker.currentNode;
      if (!(el instanceof HTMLElement)) continue;
      const isContent =
        el.matches('img, input, textarea, select, button, video, iframe, svg') ||
        Array.from(el.childNodes).some(
          (n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim().length > 0
        );
      if (!isContent) continue;
      const r = el.getBoundingClientRect();
      if (r.height < 4 || r.width < 4) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
      boxes.push({ top: r.top + window.scrollY, bottom: r.bottom + window.scrollY });
    }
    boxes.sort((a, b) => a.top - b.top);
    let cursor = null;
    for (const b of boxes) {
      if (cursor !== null && b.top - cursor > deadBandPx) {
        findings.push({
          check: 'dead-band',
          detail: `vertical gap ${Math.round(b.top - cursor)}px between y=${Math.round(cursor)} and y=${Math.round(b.top)}`,
        });
      }
      cursor = cursor === null ? b.bottom : Math.max(cursor, b.bottom);
    }
  }

  // --- nav-fit: when the horizontal link row is visible, it must not
  // wrap (row height should stay ~one line) and must fit the viewport.
  const nav = document.querySelector('header nav, nav');
  if (nav) {
    const links = Array.from(nav.querySelectorAll('a')).filter((a) => {
      const r = a.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
    if (links.length >= 3) {
      const tops = new Set(links.map((a) => Math.round(a.getBoundingClientRect().top / 8)));
      if (tops.size > 2) {
        findings.push({
          check: 'nav-fit',
          detail: `nav links wrap onto ${tops.size} bands at vw=${vw}; hamburger breakpoint is too low for the label set`,
        });
      }
      const navRect = nav.getBoundingClientRect();
      if (navRect.width > vw + 1) {
        findings.push({ check: 'nav-fit', detail: `nav row width ${Math.round(navRect.width)} exceeds vw=${vw}` });
      }
    }
  }
  return findings;
};

const browser = await chromium.launch();
let total = 0;
try {
  for (const p of paths) {
    const url = baseUrl.replace(/\/+$/, '') + (p.startsWith('/') ? p : '/' + p);
    for (const vp of VIEWPORTS) {
      const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height } });
      try {
        const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
        if (!resp || resp.status() >= 400) {
          console.error(`FAIL ${url} @${vp.width}: HTTP ${resp ? resp.status() : 'no response'}`);
          total++;
          continue;
        }
        const findings = await page.evaluate(AUDIT_FN, {
          deadBandPx: DEAD_BAND_PX,
          isDesktop: vp.desktop,
        });
        // de-dup identical findings
        const seen = new Set();
        for (const f of findings) {
          const key = `${f.check}|${f.detail}`;
          if (seen.has(key)) continue;
          seen.add(key);
          console.error(`FAIL ${url} @${vp.width} [${f.check}] ${f.detail}`);
          total++;
        }
      } catch (e) {
        console.error(`ERROR ${url} @${vp.width}: ${e.message}`);
        total++;
      } finally {
        await page.close();
      }
    }
  }
} finally {
  await browser.close();
}
if (total === 0) {
  console.log(`responsive lint clean: ${paths.length} page(s) x ${VIEWPORTS.length} viewports`);
  process.exit(0);
}
console.error(`responsive lint: ${total} finding(s)`);
process.exit(1);
