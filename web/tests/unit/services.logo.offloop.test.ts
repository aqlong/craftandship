import { describe, it, expect } from 'vitest';
import path from 'path';
import { processLogo, cleanSvgOffLoop, sanitizeSvg, LogoBusyError, InvalidLogoError } from '../../src/services/logo';
import { cleanSvg } from '../../src/services/svg_sanitize';

/**
 * `cleanSvg` (svg_sanitize.ts) is proven linear by
 * services.logo.linear-time.test.ts. This file covers the machinery around
 * it in logo.ts: the cleaning runs in a worker thread so the web process's
 * event loop is free while it runs, a cleaning that does not finish is killed
 * from outside, and a burst of uploads is bounded.
 *
 * These exercise `cleanSvgOffLoop`, which the regex version of logo.ts does
 * not have. The proof that the old code blocks the event loop is in
 * services.logo.linear-time.test.ts (its `processLogo` case runs against both
 * versions and starves its timer on the old one).
 */

const stuckWorker = path.join(__dirname, 'services.logo.stuck-svg-worker.fixture.ts');

/** A 10 ms timer for the duration of `work`: how many times it fired, and the longest gap. */
async function watchTimer<T>(
  work: () => Promise<T>
): Promise<{ outcome: T; ticks: number; longestGapMs: number; elapsedMs: number }> {
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
    const outcome = await work();
    return { outcome, ticks, longestGapMs, elapsedMs: performance.now() - started };
  } finally {
    clearInterval(timer);
  }
}

const SMALL_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#2255cc"/></svg>';

describe('cleanSvgOffLoop', () => {
  it('gives the same result as the synchronous cleaner', async () => {
    const off = await cleanSvgOffLoop(SMALL_SVG);
    expect(off).toEqual(cleanSvg(SMALL_SVG));
    expect(off?.svg).toBe(sanitizeSvg(SMALL_SVG));
    expect(off?.svg).toContain('<rect width="40" height="40" fill="#2255cc"/>');
    expect(off).toMatchObject({ width: 40, height: 40, colors: [{ red: 0x22, green: 0x55, blue: 0xcc, count: 1 }] });
  });

  it('answers null, as the cleaner does, for text that is not an SVG document', async () => {
    expect(await cleanSvgOffLoop('<html><body>hello</body></html>')).toBeNull();
  });

  it('runs off the event loop: a cleaning that never finishes is killed at the limit and timers kept firing', async () => {
    const watched = await watchTimer(() =>
      cleanSvgOffLoop('make-the-cleaner-hang', { workerFile: stuckWorker, timeoutMs: 400 }).then(
        () => 'answered',
        (err: Error) => err.message
      )
    );
    // Ended by the limit, not sooner and not much later.
    expect(watched.outcome).toMatch(/timed out after 400ms and the parser thread was terminated/);
    expect(watched.elapsedMs).toBeGreaterThanOrEqual(390);
    expect(watched.elapsedMs).toBeLessThan(3000);
    // The event loop was free the whole time: a 10 ms timer over 400 ms.
    expect(watched.ticks).toBeGreaterThanOrEqual(10);
    expect(watched.longestGapMs).toBeLessThan(200);
  });

  it('a worker that behaves is not disturbed by the kill machinery', async () => {
    // Same fixture, input without the marker: it answers normally.
    const result = await cleanSvgOffLoop(SMALL_SVG, { workerFile: stuckWorker, timeoutMs: 2000 });
    expect(result).toEqual(cleanSvg(SMALL_SVG));
  });

  it('refuses, without starting a thread, once too many uploads are already in line', async () => {
    // Fill the line with cleanings that will not finish before the assertion.
    const inFlight = Array.from({ length: 8 }, () =>
      cleanSvgOffLoop('make-the-cleaner-hang', { workerFile: stuckWorker, timeoutMs: 300 }).catch((err: Error) => err)
    );
    await expect(cleanSvgOffLoop(SMALL_SVG)).rejects.toBeInstanceOf(LogoBusyError);
    // The queued ones drain on their own (each is killed at its limit); the
    // line is open again afterwards.
    const drained = await Promise.all(inFlight);
    expect(drained.every((outcome) => outcome instanceof Error)).toBe(true);
    await expect(cleanSvgOffLoop(SMALL_SVG)).resolves.toMatchObject({ width: 40 });
  });
});

describe('processLogo over the worker path', () => {
  it('cleans and stores an SVG whose cleaning runs in the worker', async () => {
    const evil =
      '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" onload="alert(1)">' +
      '<script>alert(1)</script><rect width="40" height="40" fill="#2255cc"/></svg>';
    const result = await processLogo(Buffer.from(evil, 'utf8'), 'image/svg+xml');
    expect(result.mimetype).toBe('image/svg+xml');
    const stored = Buffer.from(result.cleanedBase64, 'base64').toString('utf8');
    expect(stored).not.toMatch(/<script|onload/i);
    expect(stored).toContain('<rect width="40" height="40" fill="#2255cc"/>');
    expect(result.width).toBe(40);
  });

  it('rejects XML-looking bytes that are not UTF-8 before any cleaning', async () => {
    // "<svg" followed by a byte that cannot occur in UTF-8.
    const notUtf8 = Buffer.from([0x3c, 0x73, 0x76, 0x67, 0xff, 0xfe]);
    const outcome = await processLogo(notUtf8, 'image/svg+xml').catch((err: Error) => err);
    expect(outcome).toBeInstanceOf(InvalidLogoError);
    expect((outcome as Error).message).toMatch(/UTF-8/);
  });
});
