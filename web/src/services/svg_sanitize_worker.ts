/**
 * Worker-thread entry point for cleaning an uploaded SVG logo.
 *
 * `logo.ts` starts this file in a `worker_threads` worker and posts it the
 * text of one SVG; it answers `{ ok: true, result }` with what `cleanSvg`
 * returns (the rebuilt document with its size and colors, or null when the
 * text is refused) or `{ ok: false, error }`. Nothing else happens here. The
 * point of the thread is the parent's ability to end it: the text comes from
 * an upload, and a cleaning that runs too long is killed from outside instead
 * of holding the web server's event loop.
 */
import { parentPort } from 'worker_threads';
import { cleanSvg, type CleanedSvg } from './svg_sanitize';

export type SvgSanitizeReply = { ok: true; result: CleanedSvg | null } | { ok: false; error: string };

if (!parentPort) {
  throw new Error('svg_sanitize_worker must be started as a worker thread');
}
const port = parentPort;

port.on('message', (svg: string) => {
  let reply: SvgSanitizeReply;
  try {
    reply = { ok: true, result: cleanSvg(svg) };
  } catch (err) {
    reply = { ok: false, error: (err as Error).message || 'unknown error' };
  }
  port.postMessage(reply);
});
