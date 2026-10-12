import { describe, it, expect } from 'vitest';
import { processLogo } from '../../src/services/logo';

/**
 * Three SVGs that are small or ordinary to read and ruinous to DRAW. Each is
 * well-formed, passes the cleaner (nothing in it is active content) and fits
 * the 2 MB upload limit. Measured on the version of logo.ts that still gave
 * the cleaned SVG to the image library (sharp: libvips with librsvg) for its
 * size and colors:
 *  - one run of 200,000 characters of text: the drawing does not return
 *    within 40 seconds;
 *  - one filled path of 150,000 curve segments: the same;
 *  - 250 masks, each masked by the one before: the library overflows its
 *    stack and the whole process dies (SIGILL on the machine it was measured
 *    on).
 * Those calls ran on the libuv thread pool, which the web process shares for
 * file access, DNS and password hashing: four such uploads and none of that
 * answers, or one of the third kind and the server is gone.
 *
 * logo.ts now never draws an SVG. Each case below is processed like any other
 * upload, in tens of milliseconds, with the event loop free. Run against the
 * earlier version (in a scratch copy), the first two time out and the third
 * kills the test process.
 */

const NS = 'xmlns="http://www.w3.org/2000/svg"';

const HOSTILE_DRAWINGS: Record<string, () => string> = {
  'one run of 200,000 characters of text': () =>
    `<svg ${NS} width="400" height="400"><text x="0" y="20" font-size="12" fill="#b45309">` +
    'logo '.repeat(40_000) +
    '</text></svg>',
  'a filled path of 150,000 curve segments': () =>
    `<svg ${NS} width="40" height="40"><path fill="#b45309" d="M1 2 ` + 'C1 9 9 1 5 5 '.repeat(150_000) + '"/></svg>',
  '250 masks, each masked by the one before': () => {
    let masks = '';
    for (let level = 0; level < 250; level += 1) {
      const previous = level ? ` mask="url(#m${level - 1})"` : '';
      masks += `<mask id="m${level}"><rect width="40" height="40" fill="#fff"${previous}/></mask>`;
    }
    return `<svg ${NS} width="40" height="40">${masks}<rect width="40" height="40" fill="#b45309" mask="url(#m249)"/></svg>`;
  },
};

/** A 10 ms timer for the duration of `work`: the longest gap between two firings. */
async function longestTimerGap(work: () => Promise<unknown>): Promise<{ longestGapMs: number; elapsedMs: number }> {
  const started = performance.now();
  let last = started;
  let longestGapMs = 0;
  const timer = setInterval(() => {
    const now = performance.now();
    longestGapMs = Math.max(longestGapMs, now - last);
    last = now;
  }, 10);
  try {
    await work();
  } finally {
    clearInterval(timer);
  }
  return { longestGapMs, elapsedMs: performance.now() - started };
}

describe('an SVG that is ruinous to draw is processed without drawing it', () => {
  for (const [name, build] of Object.entries(HOSTILE_DRAWINGS)) {
    it(`${name}: stored in well under two seconds, event loop free`, async () => {
      const upload = Buffer.from(build(), 'utf8');
      expect(upload.length).toBeLessThan(2 * 1024 * 1024);
      let result: Awaited<ReturnType<typeof processLogo>> | undefined;
      const watched = await longestTimerGap(async () => {
        result = await processLogo(upload, 'image/svg+xml');
      });
      // Tens of milliseconds in practice (thread start included); the bound
      // leaves room for a loaded machine and is still a twentieth of what the
      // drawing took before it was abandoned.
      expect(watched.elapsedMs).toBeLessThan(2000);
      expect(watched.longestGapMs).toBeLessThan(250);
      expect(result?.mimetype).toBe('image/svg+xml');
      expect(result?.colors.primary_color).toBe('#c06000');
      expect([result?.width, result?.height]).toEqual(name.startsWith('one run') ? [400, 400] : [40, 40]);
    });
  }

  it('the process and its thread pool are still answering afterwards', async () => {
    for (const build of Object.values(HOSTILE_DRAWINGS)) {
      await processLogo(Buffer.from(build(), 'utf8'), 'image/svg+xml');
    }
    // A raster upload uses the image library, and with it the thread pool.
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
      'base64'
    );
    const result = await processLogo(png, 'image/png');
    expect(result.mimetype).toBe('image/png');
    expect([result.width, result.height]).toEqual([1, 1]);
  });
});
