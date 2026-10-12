/**
 * Test fixture for services.logo.offloop.test.ts, not a test.
 *
 * An SVG-cleaning worker that answers exactly like the real one
 * (svg_sanitize_worker.ts), except that an SVG containing the marker below
 * sends it into a regular expression that never finishes. It stands in for a
 * future change to svg_sanitize.ts that is not linear on some input, so that
 * the kill behaviour in logo.ts (terminate the thread, reject, keep the event
 * loop free) can be tested without a real pathological input.
 */
import { parentPort } from 'worker_threads';
import { cleanSvg } from '../../src/services/svg_sanitize';

const MARKER = 'make-the-cleaner-hang';

if (!parentPort) {
  throw new Error('this fixture must be started as a worker thread');
}
const port = parentPort;

port.on('message', (svg: string) => {
  if (svg.includes(MARKER)) {
    // Exponential backtracking, about 2^48 steps: effectively forever.
    /^(a+)+$/.test('a'.repeat(48) + '!');
  }
  port.postMessage({ ok: true, result: cleanSvg(svg) });
});
