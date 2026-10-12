/**
 * Worker-thread entry point for the faithful-copy crawler's parsing.
 *
 * `site_copy_crawl.ts` starts this file in a `worker_threads` worker and posts
 * it one `ParseJob` at a time; it answers each with `{ ok: true, result }` or
 * `{ ok: false, error }`. Nothing else happens here. The point of the thread
 * is the parent's ability to end it: the text being parsed comes from a
 * remote site, and a parse that runs too long is killed from outside instead
 * of holding the web server's event loop.
 */
import { parentPort } from 'worker_threads';
import { runParseJob, type ParseJob, type ParseResult } from './site_copy_parse';

export type ParseWorkerReply =
  | { ok: true; result: ParseResult }
  | { ok: false; error: string };

if (!parentPort) {
  throw new Error('site_copy_parse_worker must be started as a worker thread');
}
const port = parentPort;

port.on('message', (job: ParseJob) => {
  let reply: ParseWorkerReply;
  try {
    reply = { ok: true, result: runParseJob(job) };
  } catch (err) {
    reply = { ok: false, error: (err as Error).message || 'unknown parse error' };
  }
  port.postMessage(reply);
});
