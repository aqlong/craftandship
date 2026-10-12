/**
 * Test fixture for services.site-copy-crawl.parse-worker.test.ts, not a test.
 *
 * A parser worker that answers exactly like the real one, except that text
 * containing the marker below sends it into a regular expression that never
 * finishes. It stands in for a future change to the parser that is not
 * linear on some input, so the crawler's response to that (terminate the
 * thread, report the page, carry on) can be tested through `crawlSite`.
 */
import { parentPort } from 'worker_threads';
import { runParseJob, type ParseJob } from '../../src/services/site_copy_parse';

const MARKER = 'make-the-parser-hang';

if (!parentPort) {
  throw new Error('this fixture must be started as a worker thread');
}
const port = parentPort;

port.on('message', (job: ParseJob) => {
  const text = job.kind === 'page' ? job.html : job.text;
  if (text.includes(MARKER)) {
    // Exponential backtracking, about 2^48 steps: effectively forever.
    /^(a+)+$/.test('a'.repeat(48) + '!');
  }
  port.postMessage({ ok: true, result: runParseJob(job) });
});
