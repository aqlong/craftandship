import { describe, it, expect } from 'vitest';
import { sanitizeSvg, processLogo } from '../../src/services/logo';

/**
 * The SVG cleaner must take time proportional to the length of the upload,
 * and must not run on the web process's event loop.
 *
 * Every case here fails on the version of `web/src/services/logo.ts` this replaced (the
 * regex `sanitizeSvg`): proven by running this file against a scratch copy of
 * that version, where the bound cases time out (the lazy `<?...?>`,
 * `<!DOCTYPE...>` and per-tag `<tag>...</tag>` patterns are super-linear: a
 * repeated opener with no terminator is rescanned from every opener) and the
 * responsiveness case starves its timer because the regex work is synchronous
 * on the event loop. On the fixed code every case is well under its bound.
 *
 * Linearity itself (not just one bound) is argued in svg_sanitize.ts; the
 * scaling case below is the runnable check.
 */

const SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">';
const SVG_CLOSE = '<rect width="40" height="40" fill="#cc2222"/></svg>';

/** A valid SVG of roughly `bytes`, with `unit` repeated inside it. */
function build(wrap: (filler: string) => string, unit: string, bytes: number): string {
  const envelope = SVG_OPEN.length + SVG_CLOSE.length + 32;
  return SVG_OPEN + wrap(unit.repeat(Math.max(1, Math.floor((bytes - envelope) / unit.length)))) + SVG_CLOSE;
}

const asElements = (filler: string): string => filler;
const inCdata = (filler: string): string => `<desc><![CDATA[${filler}]]></desc>`;
const raw = (filler: string): string => filler;

// Each builder makes a hostile input that the earlier regexes are super-linear
// on (confirmed by running this file against the earlier module: each case below
// runs for several seconds there). The fixed cleaner fast-rejects or reads
// each one in a few tens of ms.
const HOSTILE: Record<string, (bytes: number) => string> = {
  'self-closing script elements': (bytes) => build(asElements, '<script/>', bytes),
  'processing-instruction openers (raw)': (bytes) => raw('<?'.repeat(Math.floor(bytes / 2))),
  'doctype openers (raw)': (bytes) => raw('<!DOCTYPE '.repeat(Math.floor(bytes / 10))),
  'processing-instruction openers in CDATA': (bytes) => build(inCdata, '<?', bytes),
  'script openers in CDATA': (bytes) => build(inCdata, '<script ', bytes),
};

describe('sanitizeSvg takes time proportional to the input, not its square', () => {
  // 512 KiB: on the earlier code the slowest families run for tens of seconds (past the
  // suite timeout); on the fixed code every family is a few tens of ms.
  const BYTES = 512 * 1024;
  const BOUND_MS = 1500;

  for (const [name, make] of Object.entries(HOSTILE)) {
    it(`stays under ${BOUND_MS} ms on 512 KiB of ${name}`, () => {
      const svg = make(BYTES);
      const started = performance.now();
      sanitizeSvg(svg);
      const elapsed = performance.now() - started;
      expect(elapsed).toBeLessThan(BOUND_MS);
    });
  }

  it('four times the input takes about four times the work, not sixteen (a valid logo-shaped document)', () => {
    // Real work to completion: many kept elements, so the whole document is
    // read and written, not fast-rejected.
    const manyRects = (bytes: number): string => build(asElements, '<rect x="1" y="2" width="3" height="4" fill="#abc"/>', bytes);
    const smallSvg = manyRects(256 * 1024);
    const largeSvg = manyRects(1024 * 1024);
    // Warm both, then interleave the runs and keep the best of each, so a
    // busy machine (other test files run in parallel) slows both alike.
    sanitizeSvg(smallSvg);
    sanitizeSvg(largeSvg);
    let small = Infinity;
    let large = Infinity;
    for (let run = 0; run < 7; run += 1) {
      let started = performance.now();
      sanitizeSvg(smallSvg);
      small = Math.min(small, performance.now() - started);
      started = performance.now();
      sanitizeSvg(largeSvg);
      large = Math.min(large, performance.now() - started);
    }
    // Linear is about 4x; quadratic about 16x. The line is drawn at 9x, far
    // from both, because this is a wall-clock measurement.
    expect(large).toBeLessThan(small * 9 + 10);
    expect(large).toBeLessThan(1000);
  });
});

describe('processLogo does not block the event loop while an SVG is cleaned', () => {
  it('a 10 ms timer keeps firing while a hostile 512 KiB SVG is processed', async () => {
    // On the earlier code this runs the regex cleaner synchronously inside processLogo, so
    // the timer cannot fire for seconds; here the cleaning is on a worker
    // thread. The cleaner refuses this document (escaping what the CDATA
    // section holds would make it too much longer), so processLogo rejects:
    // the catch below swallows that. What is under test is the timer, not
    // the outcome.
    const svg = HOSTILE['script openers in CDATA'](512 * 1024);

    const started = performance.now();
    let last = started;
    let ticks = 0;
    let longestGapMs = 0;
    const timer = setInterval(() => {
      const now = performance.now();
      longestGapMs = Math.max(longestGapMs, now - last);
      last = now;
      ticks += 1;
    }, 10);
    try {
      await processLogo(Buffer.from(svg, 'utf8'), 'image/svg+xml').catch(() => undefined);
    } finally {
      clearInterval(timer);
    }
    const elapsed = performance.now() - started;

    // The loop was free throughout: the timer fired roughly on schedule and
    // never stalled for long. On the earlier code the gap is the whole regex run (seconds).
    // The bounds are loose on purpose: other test files share the machine.
    expect(longestGapMs).toBeLessThan(1000);
    expect(ticks).toBeGreaterThan((elapsed / 10) * 0.2);
  });
});
