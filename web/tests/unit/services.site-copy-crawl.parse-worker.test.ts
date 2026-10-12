import { describe, it, expect, afterAll, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  crawlSite,
  ParseWorker,
  segmentHtmlToCopyPage,
  extractInternalLinks,
  extractMediaUrls,
  extractScriptBlobUrls,
  type DnsLookupFn,
} from '../../src/services/site_copy_crawl';
import { runParseJob, type ParseJob } from '../../src/services/site_copy_parse';

/**
 * The crawler parses fetched text in a worker thread so that a parse which
 * goes wrong can be ended from outside, and gives every step of a crawl a
 * time limit that holds. These tests cover that machinery:
 *
 *  - `ParseWorker` against the real worker entry (the result is the one the
 *    parsing functions give when called directly);
 *  - `ParseWorker` against a worker that misbehaves on request: stuck in a
 *    catastrophic regular expression, out of memory, throwing, exiting;
 *  - `crawlSite` when a parse never finishes.
 *
 * The limits on the fetching side (DNS, transport, body, the crawl deadline)
 * are in services.site-copy-crawl.time-limits.test.ts.
 */

const PUBLIC_DNS: DnsLookupFn = async () => [{ address: '93.184.216.34', family: 4 }];
const ORIGIN = 'https://studio.example';

function htmlResponse(body: string, contentType = 'text/html'): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': contentType }),
    text: async () => body,
  } as unknown as Response;
}

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

// ============================================================
// A worker that misbehaves when asked to
// ============================================================

// Plain JavaScript, written to a temporary directory: a .js entry is loaded
// by the worker directly, the way the compiled build loads its own. A "media"
// job whose text is one of the commands below triggers the fault; any other
// job is answered with the id of the thread that handled it.
const FAULTY_WORKER_SOURCE = `
const { parentPort, threadId } = require('worker_threads');
parentPort.on('message', (job) => {
  const command = job.kind === 'media' ? job.text : '';
  if (command === 'catastrophic-regex') {
    // Exponential backtracking: about 2^48 steps, never finishes.
    /^(a+)+$/.test('a'.repeat(48) + '!');
  }
  if (command === 'busy-loop') {
    for (;;) { /* never yields */ }
  }
  if (command === 'out-of-memory') {
    const hoard = [];
    for (;;) hoard.push(new Array(100000).fill(hoard.length));
  }
  if (command === 'throw') throw new Error('faulty worker threw');
  if (command === 'exit') process.exit(3);
  if (command === 'report-error') {
    parentPort.postMessage({ ok: false, error: 'faulty worker reported an error' });
    return;
  }
  parentPort.postMessage({ ok: true, result: { kind: 'media', mediaUrls: ['thread:' + threadId] } });
});
`;

const scratchDir = fs.mkdtempSync(path.join(os.tmpdir(), 'site-copy-parse-worker-'));
const faultyEntry = path.join(scratchDir, 'faulty_worker.js');
fs.writeFileSync(faultyEntry, FAULTY_WORKER_SOURCE);

// The real parser plus a hang on a marker, as TypeScript next to this file,
// so it is loaded the way the built-in parser is loaded under Vitest.
const stuckParserEntry = path.join(__dirname, 'services.site-copy-crawl.stuck-parser.fixture.ts');

afterAll(() => {
  fs.rmSync(scratchDir, { recursive: true, force: true });
});

const command = (text: string): ParseJob => ({ kind: 'media', text });

async function threadOf(worker: ParseWorker): Promise<string> {
  const result = await worker.run(command('who'), 5000);
  return result.mediaUrls[0];
}

describe('ParseWorker with the real worker entry', () => {
  const workers: ParseWorker[] = [];
  afterEach(async () => {
    await Promise.all(workers.splice(0).map((worker) => worker.close()));
  });

  const page = `<html><head><title>Sample &amp; Studio</title>
    <script src="/gallery/script.js"></script></head>
    <body><nav><a href="/about">About</a><a href="/services/">Sessions</a></nav>
    <main><h1>Sample Studio</h1><p>Sessions by <b>appointment</b>.</p>
    <img src="https://studio.example/static/visible.jpg"></main></body></html>`;

  it('returns for a page exactly what the parsing functions return when called directly', async () => {
    const worker = new ParseWorker();
    workers.push(worker);
    const job: ParseJob = { kind: 'page', html: page, path: '/', origin: ORIGIN, discoverLinks: true };
    const result = await worker.run(job, 10_000);
    expect(result).toEqual(runParseJob(job));
    expect(result).toEqual({
      kind: 'page',
      links: extractInternalLinks(page, ORIGIN),
      mediaUrls: extractMediaUrls(page),
      scriptUrls: extractScriptBlobUrls(page, ORIGIN),
      page: segmentHtmlToCopyPage('/', page),
    });
    // And the content is what the fixture says, not two matching empties.
    expect(result).toMatchObject({
      links: ['/about', '/services'],
      mediaUrls: ['https://studio.example/static/visible.jpg'],
      scriptUrls: ['https://studio.example/gallery/script.js'],
      page: { title: 'Sample & Studio', markdown_blocks: ['# Sample Studio', 'Sessions by **appointment**.'] },
    });
  });

  it('does not discover links unless the job asks, and mines media from a script blob', async () => {
    const worker = new ParseWorker();
    workers.push(worker);
    const noLinks = await worker.run(
      { kind: 'page', html: page, path: '/about', origin: ORIGIN, discoverLinks: false },
      10_000
    );
    expect(noLinks).toMatchObject({ kind: 'page', links: null });
    const blob = 'var photos=["https://img1.wsimg.com/blobby/go/x/g1.jpg"];var v="https://youtu.be/ZxCvBn987654";';
    expect(await worker.run({ kind: 'media', text: blob }, 10_000)).toEqual({
      kind: 'media',
      mediaUrls: ['https://img1.wsimg.com/blobby/go/x/g1.jpg', 'https://www.youtube.com/watch?v=ZxCvBn987654'],
    });
  });

  it('refuses a second job while one is in flight', async () => {
    const worker = new ParseWorker();
    workers.push(worker);
    const first = worker.run({ kind: 'media', text: 'x' }, 10_000);
    await expect(worker.run({ kind: 'media', text: 'y' }, 10_000)).rejects.toThrow(/busy/);
    await expect(first).resolves.toEqual({ kind: 'media', mediaUrls: [] });
  });

  it('rejects the job in flight when closed, and can be closed twice', async () => {
    const worker = new ParseWorker();
    const pending = worker.run({ kind: 'media', text: 'x'.repeat(1024) }, 10_000);
    const closing = worker.close();
    await expect(pending).rejects.toThrow(/closed/);
    await closing;
    await worker.close();
  });
});

describe('ParseWorker ends a parse that does not finish', () => {
  const workers: ParseWorker[] = [];
  afterEach(async () => {
    await Promise.all(workers.splice(0).map((worker) => worker.close()));
  });

  function faulty(): ParseWorker {
    const worker = new ParseWorker(faultyEntry);
    workers.push(worker);
    return worker;
  }

  it.each(['catastrophic-regex', 'busy-loop'])(
    'a thread stuck in a %s is terminated at the time limit, and timers kept firing meanwhile',
    async (stuck) => {
      const worker = faulty();
      const before = await threadOf(worker);
      const watched = await watchTimer(() =>
        worker.run(command(stuck), 400).then(
          () => 'answered',
          (err: Error) => err.message
        )
      );
      expect(watched.outcome).toMatch(/timed out after 400ms and the parser thread was terminated/);
      // Ended by the limit, not sooner and not much later.
      expect(watched.elapsedMs).toBeGreaterThanOrEqual(390);
      expect(watched.elapsedMs).toBeLessThan(3000);
      // The calling thread was free the whole time: a 10 ms timer over 400 ms.
      expect(watched.ticks).toBeGreaterThanOrEqual(10);
      expect(watched.longestGapMs).toBeLessThan(200);
      // The next job gets a new thread, and an answer.
      const after = await threadOf(worker);
      expect(after).toMatch(/^thread:\d+$/);
      expect(after).not.toBe(before);
    }
  );

  it('reuses one thread for consecutive jobs that behave', async () => {
    const worker = faulty();
    const first = await threadOf(worker);
    expect(await threadOf(worker)).toBe(first);
    expect(await threadOf(worker)).toBe(first);
  });

  it('a thread that runs out of heap fails its job and nothing else', async () => {
    const worker = faulty();
    const before = await threadOf(worker);
    await expect(worker.run(command('out-of-memory'), 15_000)).rejects.toThrow(/memory/i);
    // This process is still here, and the worker object still works.
    const after = await threadOf(worker);
    expect(after).not.toBe(before);
  });

  it('a thread that throws, exits or reports an error rejects that job only', async () => {
    const worker = faulty();
    await expect(worker.run(command('throw'), 5000)).rejects.toThrow(/faulty worker threw/);
    await expect(worker.run(command('exit'), 5000)).rejects.toThrow(/parser thread exited \(code 3\)/);
    await expect(worker.run(command('report-error'), 5000)).rejects.toThrow(/faulty worker reported an error/);
    expect(await threadOf(worker)).toMatch(/^thread:\d+$/);
  });

  it('a worker entry that cannot be loaded is a rejection, not a crash', async () => {
    const worker = new ParseWorker(path.join(scratchDir, 'no_such_worker.js'));
    workers.push(worker);
    await expect(worker.run(command('who'), 5000)).rejects.toThrow(/no_such_worker|Cannot find module/);
  });
});

// ============================================================
// crawlSite: the parse limit, end to end
// ============================================================

describe('crawlSite when a parse does not finish', () => {
  const home = '<html><body><main><h1>Home</h1><p>Welcome in.</p></main></body></html>';

  it('control: the page used below parses fine with the default limits', async () => {
    const result = await crawlSite('https://studio.example/', {
      fetchImpl: (async () => htmlResponse(home)) as unknown as typeof fetch,
      dnsLookup: PUBLIC_DNS,
      maxPages: 1,
    });
    expect(result.warnings).toEqual([]);
    expect(result.pages.map((p) => p.markdown_blocks)).toEqual([['# Home', 'Welcome in.']]);
  });

  it('a parse that overruns its limit becomes a warning and the crawl still returns', async () => {
    // One millisecond is less than a thread takes to start, so the limit
    // always fires first; what is under test is what the crawl does then.
    const watched = await watchTimer(() =>
      crawlSite('https://studio.example/', {
        fetchImpl: (async () => htmlResponse(home)) as unknown as typeof fetch,
        dnsLookup: PUBLIC_DNS,
        maxPages: 1,
        parseTimeoutMs: 1,
      })
    );
    expect(watched.outcome.pages).toEqual([]);
    expect(watched.outcome.media_candidates).toEqual([]);
    expect(watched.outcome.warnings).toEqual([
      'parse of https://studio.example/ failed: timed out after 1ms and the parser thread was terminated',
    ]);
    expect(watched.elapsedMs).toBeLessThan(3000);
  });

  it('a parser that hangs on one page and one script: those two become warnings, the rest of the site is captured', async () => {
    // The stand-in parser (see the fixture) is the real one plus a regular
    // expression that never finishes on text containing the marker: what a
    // non-linear pattern added to the parser later would look like from here.
    const bodies: Record<string, [string, string]> = {
      'https://studio.example/': [
        'text/html',
        '<script src="/gallery/stuck.js"></script><script src="/gallery/fine.js"></script>' +
          '<a href="/about">About</a><a href="/services">Services</a>' +
          '<h1>Home</h1><p>Welcome in.</p><img src="https://studio.example/static/home.jpg">',
      ],
      'https://studio.example/about': ['text/html', '<h1>About</h1><p>make-the-parser-hang</p>'],
      'https://studio.example/services': ['text/html', '<h1>Services</h1><p>What we do.</p>'],
      'https://studio.example/gallery/stuck.js': ['text/javascript', 'var x = "make-the-parser-hang";'],
      'https://studio.example/gallery/fine.js': [
        'text/javascript',
        'var photos = ["https://studio.example/static/g1.png"];',
      ],
    };
    const fetchImpl = (async (url: string | URL) => {
      const [contentType, body] = bodies[String(url)];
      return htmlResponse(body, contentType);
    }) as unknown as typeof fetch;

    const watched = await watchTimer(() =>
      crawlSite('https://studio.example/', {
        fetchImpl,
        dnsLookup: PUBLIC_DNS,
        parseTimeoutMs: 2000,
        parseWorkerFile: stuckParserEntry,
      })
    );
    const result = watched.outcome;
    expect(result.pages.map((p) => [p.path, p.markdown_blocks])).toEqual([
      ['/', ['# Home', 'Welcome in.']],
      ['/services', ['# Services', 'What we do.']],
    ]);
    expect(result.warnings).toEqual([
      'parse of https://studio.example/about failed: timed out after 2000ms and the parser thread was terminated',
      'parse of script blob https://studio.example/gallery/stuck.js failed: timed out after 2000ms and the parser thread was terminated',
    ]);
    expect(result.media_candidates).toEqual([
      'https://studio.example/static/home.jpg',
      'https://studio.example/static/g1.png',
    ]);
    // Two limits were waited out in full, and little else.
    expect(watched.elapsedMs).toBeGreaterThanOrEqual(3990);
    expect(watched.elapsedMs).toBeLessThan(12_000);
    // While the parser thread was stuck, twice, this thread's timers ran.
    expect(watched.ticks).toBeGreaterThanOrEqual(100);
    expect(watched.longestGapMs).toBeLessThan(300);
  });
});
