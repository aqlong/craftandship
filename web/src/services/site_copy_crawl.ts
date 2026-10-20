/**
 * Faithful-copy CRAWL stage.
 *
 * Fetches a customer's existing live website, segments visible content
 * by page, and emits an array of `CopyPage` objects ready to attach to
 * `_harbor_meta.copy_pages`. The downstream translator + compose
 * pipeline use that array to short-circuit archetype default page
 * composition for any path the crawler captured, so the rebuild
 * mirrors the source verbatim.
 *
 * This file is the fetching half: what may be requested, how the connection
 * is made, and the limits on a crawl. Reading what came back (the HTML
 * segmenter, link discovery, media mining) is `site_copy_parse.ts`, which
 * runs in a worker thread.
 *
 * Contract
 * ========
 * Input  : a public http(s) URL pointing at the source site's home
 *          page, plus optional `fetchImpl` (default: the pinned transport,
 *          `createPinnedFetch`), `maxPages` (default 6), `userAgent`
 *          (default identifies CAS).
 * Output : { source_url, pages: CopyPage[], warnings: string[],
 *          media_candidates: string[] }.
 *
 * Hard guarantees
 * ===============
 *  - Plain HTTP GET only (no Playwright / no headless Chrome). The vast
 *    majority of customer sites in our ICP (healers, coaches, solo
 *    practitioners, local-service tradespeople) ship server-rendered
 *    HTML that is fully readable from a single GET.
 *  - Same-origin link discovery only: links found on the start page that
 *    point at another origin are ignored.
 *  - Server-side request forgery guard. The URL comes from an admin, and
 *    redirect targets and script URLs come from the remote site, so every
 *    request (the first one and every redirect hop) must pass all of:
 *      1. http or https, no credentials in the URL;
 *      2. port 80 or 443 only (no reaching Redis, an admin port or a
 *         metadata proxy through a "website" URL);
 *      3. the host is not, textually, a private, loopback, link-local or
 *         reserved address or an internal-only name;
 *      4. every address the name resolves to is public;
 *      5. the socket is opened to one of the addresses that check 4 just
 *         validated. The default transport (`createPinnedFetch`) resolves
 *         the name inside the connection's own lookup hook, so there is no
 *         second, unchecked resolution between "validated" and "connected"
 *         (DNS rebinding), and it re-checks the peer address of the
 *         connected socket as a last assertion.
 *    Redirects are followed by hand, at most 3 hops, and only within the
 *    same site (the same host, with or without a leading "www."): that
 *    covers example.com -> www.example.com and http -> https. A redirect to
 *    any other host is reported as a warning naming the target, so the
 *    operator can crawl that address directly if it is the real site.
 *  - Bounded in size: at most `maxPages` pages, response headers capped at
 *    16 KiB, response body capped at 2 MiB after decompression (the stream
 *    is cancelled once the cap is reached, so memory use is bounded too and
 *    a compressed bomb cannot expand past it). What comes out is bounded as
 *    well: 60 blocks of at most 8000 characters per page, page paths of at
 *    most 200 characters, at most 40 media URLs of at most 2048 characters.
 *  - Bounded in time: a request is abandoned 15s after it starts, wherever
 *    it is by then (the DNS check, a redirect hop, the body read), and the
 *    crawl as a whole has a 60s deadline: every request and every parse is
 *    given at most what is left of it, so `crawlSite` returns within the
 *    deadline plus scheduling slack.
 *  - Nothing the remote site sent is parsed on the server's event loop. The
 *    text goes to a worker thread (`ParseWorker`, which runs
 *    `site_copy_parse.ts`). A parse that has not answered within 10s, or
 *    within what is left of the deadline, is ended with `worker.terminate()`
 *    and becomes a warning; so does one that outgrows the thread's 256 MB
 *    heap. The parsing code itself takes time proportional to the page's
 *    length (that file says how); the thread and the kill are what still
 *    holds if a later change to it does not. The event loop matters because
 *    one web process serves everything, the companion's crisis handler
 *    included, and the two timers above cannot fire while it is blocked.
 *  - Deterministic ordering: home page first, remaining pages sorted
 *    by alphabetical path so the same input produces the same spec.
 *  - **Brand-voice em-dash carve-out**: text returned by this module
 *    is verbatim customer copy. Em dashes are preserved deliberately.
 *    Callers must NOT post-process the markdown blocks through any
 *    em-dash sanitizer.
 *
 * What this module does NOT do
 * ============================
 *  - Does not render JavaScript. Single-page applications, JS-rendered
 *    Squarespace fluff, or any page whose visible content lives behind
 *    `fetch()` calls return empty markdown_blocks. Caller surfaces a
 *    warning and falls back to manual entry.
 *  - Does not download images. Brand assets, logos, and hero photos are
 *    out of scope; the rebuild ships with archetype default imagery until
 *    the keeper uploads their own through admin. Media URLs found in the
 *    pages and their script blobs are only listed (`media_candidates`) for
 *    an operator to approve.
 *  - Does not crawl protected content. Pages behind login or paywalls
 *    return whatever HTML the unauthenticated GET produces.
 *  - Does not parse robots.txt. We are a one-shot import on a URL the
 *    customer explicitly handed us; politeness rules don't apply the
 *    same way they do for a search crawler.
 */

import { promises as dnsPromises } from 'dns';
import http from 'http';
import https from 'https';
import net from 'net';
import path from 'path';
import { Readable, pipeline } from 'stream';
import { Worker } from 'worker_threads';
import zlib from 'zlib';
import type { CopyPage } from './harbor_to_compose';
import {
  MAX_MEDIA_CANDIDATES,
  normalizePath,
  type PageParseResult,
  type ParseJob,
  type ParseResult,
} from './site_copy_parse';
import type { ParseWorkerReply } from './site_copy_parse_worker';

// The parsing functions live in site_copy_parse.ts. They are re-exported so
// this module stays the one entry point its importers already use. Server
// code must not call them on fetched text directly: `crawlSite` runs them in
// a worker thread, and that is the only way hostile input should reach them.
export {
  extractInternalLinks,
  extractMediaUrls,
  extractScriptBlobUrls,
  normalizePath,
  segmentHtmlToCopyPage,
} from './site_copy_parse';

/**
 * Custom DNS resolver. Must mirror the shape of `dns.promises.lookup`
 * with `{ all: true }`: return ALL addresses the hostname resolves to
 * (so the guard rejects when ANY resolved address is private). Tests
 * inject this to simulate rebinding attacks without hitting the network.
 */
export type DnsLookupFn = (
  hostname: string
) => Promise<Array<{ address: string; family: number }>>;

export interface CrawlOptions {
  /**
   * Replace the transport (tests pass canned responses). When omitted, the
   * crawl uses `createPinnedFetch`, which only connects to addresses the SSRF
   * guard validated. A replacement still goes through checks 1 to 4 for every
   * hop, but check 5 (the connection itself) is then the replacement's job.
   */
  fetchImpl?: typeof fetch;
  /**
   * Override the DNS lookup used by the SSRF guard. Defaults to
   * `dns.promises.lookup(host, { all: true })`. Tests pass a stub that
   * returns canned IPs (typically a public address like 93.184.216.34)
   * so fixture hostnames such as `studio.example` don't actually have
   * to resolve.
   */
  dnsLookup?: DnsLookupFn;
  /** Hard cap on pages crawled. Default 6. Includes the home page. */
  maxPages?: number;
  /**
   * Per-request timeout in ms, from the DNS check to the last body byte.
   * Default 15000.
   */
  requestTimeoutMs?: number;
  /**
   * Deadline for the whole crawl in ms. Default 60000. Requests and parses
   * are each given at most what is left of it.
   */
  totalTimeoutMs?: number;
  /**
   * How long the parser thread may spend on one fetched body, starting the
   * thread included, before it is terminated. Default 10000.
   */
  parseTimeoutMs?: number;
  /**
   * Worker entry file to run in place of the built-in parser. Tests use it to
   * stand in a parser that hangs on one page; nothing else should set it.
   */
  parseWorkerFile?: string;
  /** Cap on response body bytes. Default 2 MiB. */
  maxBodyBytes?: number;
  /** User-Agent header. Default identifies CAS Harbor. */
  userAgent?: string;
}

export interface CrawlResult {
  source_url: string;
  pages: CopyPage[];
  warnings: string[];
  /**
   * Media URLs mined from same-site widget/script blobs: on a
   * website-builder page the static HTML can expose a single image while
   * the page's script blobs hold the gallery photos and videos. Candidates are
   * operator-approvable, never auto-included in the build.
   */
  media_candidates: string[];
}

const DEFAULT_MAX_PAGES = 6;
const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
const DEFAULT_TOTAL_TIMEOUT_MS = 60_000;
// The parser's time grows in step with the body, and the slowest full-size
// (2 MiB) body measured took about a third of a second, so ten seconds only
// ever ends a parse that has gone wrong. The limit also covers starting the
// thread, which is slowest when the code runs from TypeScript source.
const DEFAULT_PARSE_TIMEOUT_MS = 10_000;
const DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024;
const DEFAULT_USER_AGENT =
  'CraftAndShip-Harbor/1.0 (+https://craftandship.io; faithful-copy crawl)';

/** Redirect hops followed per request (each one is validated first). */
const MAX_REDIRECT_HOPS = 3;
/** Upper bound on the response header block the transport will accept. */
const MAX_RESPONSE_HEADER_BYTES = 16 * 1024;
/** Destination ports the crawler may connect to. */
const ALLOWED_PORTS: ReadonlySet<number> = new Set([80, 443]);

const MAX_SCRIPT_BLOBS = 5;
/**
 * Longest page path the crawl starts from, follows or reports. The
 * translator that reads `copy_pages` keeps 200 characters of a path
 * (`normalizeCopyPath` in harbor_to_compose.ts) and would publish a longer
 * one under a truncated address, so longer paths are left out here.
 */
const MAX_PAGE_PATH_LENGTH = 200;
/**
 * Longest script URL fetched and longest media URL reported. What the remote
 * site wrote decides how long these are, and they go into warnings and into
 * the stored result.
 */
const MAX_ASSET_URL_LENGTH = 2048;
/** Heap ceiling of the parser thread. Past it the thread ends, not the server. */
const PARSE_WORKER_MAX_HEAP_MB = 256;

/**
 * Public entry. Returns a CrawlResult; never throws on transient
 * network or parse errors (warnings list captures them). The caller
 * decides how to surface failures.
 */
export async function crawlSite(
  rawUrl: string,
  opts: CrawlOptions = {}
): Promise<CrawlResult> {
  const warnings: string[] = [];
  const startUrl = parseHttpUrl(rawUrl);
  if (!startUrl) {
    return {
      source_url: typeof rawUrl === 'string' ? rawUrl : '',
      pages: [],
      warnings: ['source_url must be an absolute http(s) URL'],
      media_candidates: [],
    };
  }

  const startProblem = crawlTargetProblem(startUrl);
  if (startProblem) {
    return {
      source_url: startUrl.toString(),
      pages: [],
      warnings: [`source_url rejected: ${startProblem}`],
      media_candidates: [],
    };
  }
  const homePath = normalizePath(startUrl.pathname);
  if (homePath.length > MAX_PAGE_PATH_LENGTH) {
    return {
      source_url: startUrl.toString(),
      pages: [],
      warnings: [`source_url rejected: its path is longer than ${MAX_PAGE_PATH_LENGTH} characters`],
      media_candidates: [],
    };
  }

  const dnsLookup = opts.dnsLookup ?? defaultDnsLookup;
  const fetchImpl = opts.fetchImpl ?? createPinnedFetch({ dnsLookup });

  const maxPages = clampInt(opts.maxPages, 1, 20, DEFAULT_MAX_PAGES);
  const requestTimeoutMs = clampInt(
    opts.requestTimeoutMs,
    1000,
    60_000,
    DEFAULT_REQUEST_TIMEOUT_MS
  );
  const totalTimeoutMs = clampInt(
    opts.totalTimeoutMs,
    5000,
    300_000,
    DEFAULT_TOTAL_TIMEOUT_MS
  );
  const parseTimeoutMs = clampInt(
    opts.parseTimeoutMs,
    1,
    60_000,
    DEFAULT_PARSE_TIMEOUT_MS
  );
  const maxBodyBytes = clampInt(
    opts.maxBodyBytes,
    1024,
    20 * 1024 * 1024,
    DEFAULT_MAX_BODY_BYTES
  );
  const userAgent = opts.userAgent ?? DEFAULT_USER_AGENT;

  const startedAt = Date.now();
  const timeLeft = (): number => startedAt + totalTimeoutMs - Date.now();

  // Resolve the start hostname once before the fetch loop, so a public
  // name that points at 169.254.169.254 (or any private address) is
  // reported up front as one clear warning instead of one per page. This
  // is an early exit, not the guard: every request re-validates, and the
  // default transport validates again at connect time. A resolver that
  // never answers is given the request timeout, like any other request.
  let startHostCheck: { ok: true } | { ok: false; reason: string };
  try {
    startHostCheck = await settleWithin(
      assertResolvedHostIsPublic(startUrl.hostname, dnsLookup),
      Math.min(requestTimeoutMs, totalTimeoutMs),
      'DNS lookup'
    );
  } catch (err) {
    startHostCheck = { ok: false, reason: (err as Error).message };
  }
  if (!startHostCheck.ok) {
    return {
      source_url: startUrl.toString(),
      pages: [],
      warnings: [`DNS-rebinding guard rejected ${startUrl.hostname}: ${startHostCheck.reason}`],
      media_candidates: [],
    };
  }

  const parser = new ParseWorker(opts.parseWorkerFile);
  const run: CrawlRun = {
    startUrl,
    homePath,
    maxPages,
    totalTimeoutMs,
    warnings,
    timeLeft,
    fetchText: (url, allowScript) =>
      fetchHtml(url, {
        fetchImpl,
        requestTimeoutMs: Math.max(1, Math.min(requestTimeoutMs, timeLeft())),
        maxBodyBytes,
        userAgent,
        dnsLookup,
        allowScript,
      }),
    parse: async (job, label) => {
      const budget = Math.min(parseTimeoutMs, timeLeft());
      if (budget <= 0) {
        warnings.push(`parse of ${label} skipped: crawl total timeout (${totalTimeoutMs}ms) reached`);
        return null;
      }
      try {
        return await parser.run(job, budget);
      } catch (err) {
        warnings.push(`parse of ${label} failed: ${(err as Error).message || 'unknown error'}`);
        return null;
      }
    },
  };

  try {
    const captured = await crawlPages(run);
    if (captured.size === 0) {
      return {
        source_url: startUrl.toString(),
        pages: [],
        warnings: warnings.length > 0 ? warnings : ['no pages fetched'],
        media_candidates: [],
      };
    }
    const mediaCandidates = await collectMediaCandidates(run, captured);
    return {
      source_url: startUrl.toString(),
      pages: orderedCopyPages(captured, run.homePath),
      warnings,
      media_candidates: mediaCandidates,
    };
  } finally {
    await parser.close();
  }
}

/** What one crawl carries from step to step. */
interface CrawlRun {
  startUrl: URL;
  /** Normalized path of the start page. */
  homePath: string;
  maxPages: number;
  totalTimeoutMs: number;
  warnings: string[];
  /** Milliseconds until the crawl's deadline; zero or less once it has passed. */
  timeLeft: () => number;
  /** One guarded request, given the request timeout or what is left of the deadline. */
  fetchText: (url: string, allowScript: boolean) => Promise<string>;
  /**
   * Parse fetched text in the parser thread. Resolves to null, after adding a
   * warning that names `label`, when the parse failed or ran out of time.
   */
  parse: (job: ParseJob, label: string) => Promise<ParseResult | null>;
}

/**
 * Fetch and parse the start page, then the pages it links to. Returns what
 * was captured, keyed by path, in the order the pages were fetched.
 */
async function crawlPages(run: CrawlRun): Promise<Map<string, PageParseResult>> {
  const origin = run.startUrl.origin;
  const captured = new Map<string, PageParseResult>();
  const queue: string[] = [run.homePath];
  const seen = new Set<string>(queue);

  while (queue.length > 0 && captured.size < run.maxPages) {
    if (run.timeLeft() <= 0) {
      run.warnings.push(
        `crawl total timeout (${run.totalTimeoutMs}ms) reached at ${captured.size} pages`
      );
      break;
    }
    const pagePath = queue.shift()!;
    const url = origin + pagePath;
    let html: string;
    try {
      html = await run.fetchText(url, false);
    } catch (err) {
      run.warnings.push(
        `fetch ${url} failed: ${(err as Error).message || 'unknown error'}`
      );
      continue;
    }

    // Discover same-origin internal links from the start page only.
    // This keeps the crawl shape predictable: home + everything home
    // links to. Going deeper (about → its own outlinks) explodes scope
    // for marginal gain since healer/coach sites are flat by design.
    const parsed = await run.parse(
      { kind: 'page', html, path: pagePath, origin, discoverLinks: pagePath === run.homePath },
      url
    );
    if (!parsed || parsed.kind !== 'page') continue;
    captured.set(pagePath, parsed);

    let overlong = 0;
    for (const linkPath of parsed.links ?? []) {
      if (seen.has(linkPath)) continue;
      if (captured.size + queue.length >= run.maxPages) break;
      if (linkPath.length > MAX_PAGE_PATH_LENGTH) {
        overlong += 1;
        continue;
      }
      seen.add(linkPath);
      queue.push(linkPath);
    }
    if (overlong > 0) {
      run.warnings.push(
        `${overlong} linked path(s) longer than ${MAX_PAGE_PATH_LENGTH} characters were not crawled`
      );
    }
  }
  return captured;
}

/**
 * Deep-media mining: media URLs (images + YouTube/Vimeo) found in the crawled
 * pages, then in the same-origin / allowlisted-CDN script blobs those pages
 * reference, which hold what the static HTML never exposes. Bounded:
 * MAX_SCRIPT_BLOBS fetches, body-size capped, candidates deduped and capped
 * at MAX_MEDIA_CANDIDATES, and no script or media URL longer than
 * MAX_ASSET_URL_LENGTH.
 */
async function collectMediaCandidates(
  run: CrawlRun,
  captured: Map<string, PageParseResult>
): Promise<string[]> {
  const candidates = new Set<string>();
  const keep = (mediaUrls: string[]): void => {
    for (const mediaUrl of mediaUrls) {
      if (mediaUrl.length <= MAX_ASSET_URL_LENGTH) candidates.add(mediaUrl);
    }
  };
  // Pages themselves also carry minable media references.
  for (const parsed of captured.values()) keep(parsed.mediaUrls);
  const scriptUrls: string[] = [];
  const seenScripts = new Set<string>();
  for (const parsed of captured.values()) {
    for (const scriptUrl of parsed.scriptUrls) {
      if (scriptUrl.length > MAX_ASSET_URL_LENGTH) continue;
      if (seenScripts.has(scriptUrl)) continue;
      seenScripts.add(scriptUrl);
      scriptUrls.push(scriptUrl);
    }
  }
  for (const scriptUrl of scriptUrls.slice(0, MAX_SCRIPT_BLOBS)) {
    if (run.timeLeft() <= 0) {
      run.warnings.push('script-blob mining stopped: total timeout reached');
      break;
    }
    let body: string;
    try {
      body = await run.fetchText(scriptUrl, true);
    } catch (err) {
      run.warnings.push(
        `script blob ${scriptUrl} failed: ${(err as Error).message || 'unknown error'}`
      );
      continue;
    }
    const mined = await run.parse({ kind: 'media', text: body }, `script blob ${scriptUrl}`);
    if (mined) keep(mined.mediaUrls);
  }
  return [...candidates].slice(0, MAX_MEDIA_CANDIDATES);
}

/** copy_pages in deterministic order: home first, the others by path. */
function orderedCopyPages(
  captured: Map<string, PageParseResult>,
  homePath: string
): CopyPage[] {
  const orderedPaths = [
    homePath,
    ...[...captured.keys()]
      .filter((p) => p !== homePath)
      .sort((a, b) => a.localeCompare(b)),
  ];
  const pages: CopyPage[] = [];
  for (const pagePath of orderedPaths) {
    const page = captured.get(pagePath)?.page;
    if (page) pages.push(page);
  }
  return pages;
}

/**
 * `work`, or a rejection once `ms` have passed. For a step that cannot be
 * cancelled (a DNS lookup): the wait ends on time even though the step does
 * not.
 */
function settleWithin<T>(work: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${what} timed out after ${ms}ms`)), ms);
    work.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err);
      }
    );
  });
}

// ============================================================
// Parsing off the event loop
// ============================================================

/**
 * What the parser thread runs when this module is itself running from
 * TypeScript source (ts-node in development, Vitest in tests): the worker
 * entry is then a .ts file, which a bare worker thread cannot load, so the
 * thread first registers ts-node's transpiler (no type check, no tsconfig
 * lookup). The compiled build never takes this path: there the entry is
 * dist/services/site_copy_parse_worker.js and is loaded directly.
 */
const TS_SOURCE_WORKER_BOOTSTRAP = `
const { workerData } = require('worker_threads');
const requireFromEntry = require('module').createRequire(workerData.entryFile);
let tsNode;
try {
  tsNode = requireFromEntry('ts-node');
} catch (err) {
  throw new Error(
    'site-copy parser: running from TypeScript source needs ts-node (a devDependency); ' +
      'the compiled build loads the compiled worker instead'
  );
}
tsNode.register({
  transpileOnly: true,
  skipProject: true,
  compilerOptions: { module: 'commonjs', target: 'es2020', esModuleInterop: true },
});
requireFromEntry(workerData.entryFile);
`;

/** The worker entry that sits next to this file: .js when compiled, .ts from source. */
function defaultParseWorkerEntry(): string {
  return path.join(__dirname, `site_copy_parse_worker${path.extname(__filename)}`);
}

interface PendingParse {
  resolve: (result: ParseResult) => void;
  reject: (reason: Error) => void;
  timer: NodeJS.Timeout;
}

/**
 * The parser thread of one crawl. `run` posts a job to it and waits, for a
 * bounded time, for the answer; the thread is started on first use and reused
 * for the crawl's later jobs.
 *
 * The bound is enforced from outside the thread: when the timer fires the
 * thread is terminated, which stops JavaScript and a running regular
 * expression alike, and the next job starts a fresh one. So the wait ends on
 * time whatever the code inside the thread is doing when its time is up.
 * Exported for tests, which also use `entryFile` to point it at a worker
 * that never answers.
 */
export class ParseWorker {
  private worker: Worker | null = null;
  private pending: PendingParse | null = null;

  constructor(private readonly entryFile: string = defaultParseWorkerEntry()) {}

  /**
   * Resolves with the parse result. Rejects when the thread reports an
   * error, dies (out of memory included), cannot be started, or has not
   * answered within `timeoutMs`. One job at a time.
   */
  run(job: ParseJob, timeoutMs: number): Promise<ParseResult> {
    if (this.pending) return Promise.reject(new Error('parser thread is busy'));
    return new Promise<ParseResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.abandon(new Error(`timed out after ${timeoutMs}ms and the parser thread was terminated`));
      }, timeoutMs);
      this.pending = { resolve, reject, timer };
      try {
        (this.worker ?? this.start()).postMessage(job);
      } catch (err) {
        this.abandon(err as Error);
      }
    });
  }

  /** Ends the thread. A job still in flight is rejected. */
  async close(): Promise<void> {
    const worker = this.worker;
    this.abandon(new Error('parser thread closed'));
    if (worker) await worker.terminate();
  }

  private start(): Worker {
    const options = {
      // Not inherited from the process: ts-node and Vitest put entries of
      // their own in process.execArgv.
      execArgv: [],
      resourceLimits: { maxOldGenerationSizeMb: PARSE_WORKER_MAX_HEAP_MB },
    };
    const worker =
      path.extname(this.entryFile) === '.ts'
        ? new Worker(TS_SOURCE_WORKER_BOOTSTRAP, {
            ...options,
            eval: true,
            workerData: { entryFile: this.entryFile },
          })
        : new Worker(this.entryFile, options);
    // Events from a thread this object has already let go of are ignored.
    worker.on('message', (reply: ParseWorkerReply) => {
      if (this.worker !== worker || !this.pending) return;
      const pending = this.pending;
      this.pending = null;
      clearTimeout(pending.timer);
      if (reply.ok) pending.resolve(reply.result);
      else pending.reject(new Error(reply.error));
    });
    worker.on('error', (err: Error) => {
      if (this.worker === worker) this.abandon(err);
    });
    worker.on('exit', (code: number) => {
      if (this.worker === worker) this.abandon(new Error(`parser thread exited (code ${code})`));
    });
    this.worker = worker;
    return worker;
  }

  /** Terminate the thread, if there is one, and reject the job in flight, if there is one. */
  private abandon(reason: Error): void {
    const worker = this.worker;
    this.worker = null;
    if (worker) void worker.terminate();
    const pending = this.pending;
    this.pending = null;
    if (pending) {
      clearTimeout(pending.timer);
      pending.reject(reason);
    }
  }
}

// ============================================================
// HTML fetch + body-size cap
// ============================================================

interface FetchOpts {
  fetchImpl: typeof fetch;
  requestTimeoutMs: number;
  maxBodyBytes: number;
  userAgent: string;
  dnsLookup: DnsLookupFn;
  /** Accept JS/JSON content-types (script-blob mining only). */
  allowScript?: boolean;
}

/**
 * Why this URL must not be requested, or null when it may be. Syntactic
 * checks only (no DNS): scheme, embedded credentials, textually private
 * host, port. Exported for tests.
 */
export function crawlTargetProblem(target: URL): string | null {
  if (target.protocol !== 'http:' && target.protocol !== 'https:') {
    return `unsupported protocol: ${target.protocol}`;
  }
  if (target.username || target.password) {
    return 'credentials in the URL are not allowed';
  }
  if (isPrivateOrLoopbackHost(target.hostname)) {
    return `private host blocked: ${target.hostname}`;
  }
  const port = effectivePort(target);
  if (!ALLOWED_PORTS.has(port)) {
    return `port ${port} is not allowed (only 80 and 443)`;
  }
  return null;
}

function effectivePort(target: URL): number {
  if (target.port) return Number(target.port);
  return target.protocol === 'https:' ? 443 : 80;
}

/**
 * True when two hostnames are the same site for redirect purposes: equal, or
 * equal once a leading "www." is dropped from either. Exported for tests.
 */
export function isSameSiteHost(a: string, b: string): boolean {
  const bare = (host: string): string => host.toLowerCase().replace(/\.$/, '').replace(/^www\./, '');
  return bare(a) === bare(b);
}

/**
 * Gate for one request. `redirectedFrom` is the URL the caller originally
 * asked for when `target` came out of a Location header, null for the first
 * request. Throws with the reason; never returns a verdict the caller could
 * forget to check.
 */
async function assertFetchAllowed(
  target: URL,
  dnsLookup: DnsLookupFn,
  redirectedFrom: URL | null
): Promise<void> {
  const problem = crawlTargetProblem(target);
  if (problem) {
    throw new Error(redirectedFrom ? `redirect refused: ${problem}` : problem);
  }
  if (redirectedFrom && !isSameSiteHost(redirectedFrom.hostname, target.hostname)) {
    throw new Error(
      `redirect to a different site was not followed: ${target.hostname} ` +
        '(crawl that address directly if it is the real site)'
    );
  }
  const resolved = await assertResolvedHostIsPublic(target.hostname, dnsLookup);
  if (!resolved.ok) {
    throw new Error(
      `${redirectedFrom ? 'redirect ' : ''}DNS-rebinding guard rejected ${target.hostname}: ${resolved.reason}`
    );
  }
}

/**
 * One guarded GET, abandoned `opts.requestTimeoutMs` after it starts. The
 * timer aborts the transport (which closes the socket) and also ends the
 * wait directly, because a step may be in flight that an abort cannot reach:
 * the DNS check is one, and a replacement `fetchImpl` that ignores the
 * signal is another.
 */
async function fetchHtml(url: string, opts: FetchOpts): Promise<string> {
  const controller = new AbortController();
  let timer: NodeJS.Timeout | undefined;
  const timedOut = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error(`timed out after ${opts.requestTimeoutMs}ms`));
    }, opts.requestTimeoutMs);
  });
  try {
    return await Promise.race([fetchHtmlBody(url, opts, controller.signal), timedOut]);
  } finally {
    clearTimeout(timer);
  }
}

async function fetchHtmlBody(url: string, opts: FetchOpts, signal: AbortSignal): Promise<string> {
  const requested = new URL(url);
  let current = requested;
  let res: Response;
  // Redirects are followed by hand (redirect: 'manual') so that every hop,
  // the first one included, passes assertFetchAllowed before any request is
  // made for it. Letting the transport follow redirects would hand the
  // remote site a way to point us at an address nothing has checked.
  for (let hop = 0; ; hop += 1) {
    await assertFetchAllowed(current, opts.dnsLookup, hop === 0 ? null : requested);
    // The check above can outlive the request's timeout (a slow resolver);
    // nothing is sent for a request that has already been given up on.
    signal.throwIfAborted();
    res = await opts.fetchImpl(current.toString(), {
      method: 'GET',
      redirect: 'manual',
      signal,
      headers: {
        'User-Agent': opts.userAgent,
        Accept: 'text/html,application/xhtml+xml',
      },
    });
    if (res.status < 300 || res.status >= 400) break;
    const location = res.headers.get('location');
    if (!location) break;
    // The redirect response's own body is never needed; release it.
    await discardBody(res);
    if (hop >= MAX_REDIRECT_HOPS) {
      throw new Error(`too many redirects (more than ${MAX_REDIRECT_HOPS})`);
    }
    try {
      current = new URL(location, current);
    } catch {
      throw new Error(`invalid redirect Location: ${location.slice(0, 200)}`);
    }
  }
  if (!res.ok) {
    await discardBody(res);
    throw new Error(`HTTP ${res.status}`);
  }
  const ct = res.headers.get('content-type') || '';
  const ctOk = opts.allowScript
    ? /html|xml|javascript|ecmascript|json|text\/plain/i.test(ct)
    : /html|xml/i.test(ct);
  if (ct && !ctOk) {
    await discardBody(res);
    throw new Error(`unsupported content-type: ${ct.slice(0, 100)}`);
  }
  // Honor the cap by truncating; segmentation still gets useful content
  // from the head + first sections.
  return await readBodyCapped(res, opts.maxBodyBytes);
}

/** Cancel a response body we are not going to read, so its socket is freed. */
async function discardBody(res: Response): Promise<void> {
  const body = res.body as ReadableStream<Uint8Array> | null | undefined;
  if (body && typeof body.cancel === 'function') {
    await body.cancel().catch(() => undefined);
  }
}

/**
 * Read at most `maxBytes` of the response body as UTF-8 text. The stream is
 * cancelled as soon as the cap is reached, so a hostile server cannot make us
 * buffer an unbounded body (response.text() reads everything first and only
 * then could be truncated). Responses without a stream (test doubles) fall
 * back to text() plus truncation.
 */
async function readBodyCapped(res: Response, maxBytes: number): Promise<string> {
  const stream = res.body as ReadableStream<Uint8Array> | null | undefined;
  if (!stream || typeof stream.getReader !== 'function') {
    const text = await res.text();
    return text.length > maxBytes ? text.slice(0, maxBytes) : text;
  }
  const reader = stream.getReader();
  const chunks: Buffer[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(Buffer.from(value.buffer, value.byteOffset, value.byteLength));
      total += value.byteLength;
      if (total >= maxBytes) {
        await reader.cancel().catch(() => undefined);
        break;
      }
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks).subarray(0, maxBytes).toString('utf8');
}

// ============================================================
// URL + path helpers
// ============================================================

/** Parse an absolute http(s) URL, or null. Policy checks are crawlTargetProblem's job. */
function parseHttpUrl(raw: unknown): URL | null {
  if (typeof raw !== 'string') return null;
  const v = raw.trim();
  if (!v) return null;
  try {
    const u = new URL(v);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u;
  } catch {
    return null;
  }
}

// Address ranges the crawler must never connect to. net.BlockList understands
// IPv4-mapped IPv6 (::ffff:10.0.0.1), which a hand-rolled prefix check misses.
// The IPv6 transition ranges are listed whole because each one embeds an IPv4
// address that a translator or relay on the path may dial; for a literal
// address in a URL this list is the only check (no DNS answer is involved).
const PRIVATE_ADDRESS_RANGES = (() => {
  const list = new net.BlockList();
  const v4: Array<[string, number]> = [
    ['0.0.0.0', 8], // "this" network
    ['10.0.0.0', 8], // RFC1918
    ['100.64.0.0', 10], // carrier-grade NAT (shared address space)
    ['127.0.0.0', 8], // loopback
    ['169.254.0.0', 16], // link-local + cloud metadata
    ['172.16.0.0', 12], // RFC1918
    ['192.0.0.0', 24], // IETF protocol assignments
    ['192.0.2.0', 24], // documentation (TEST-NET-1)
    ['192.88.99.0', 24], // 6to4 relay anycast (deprecated)
    ['192.168.0.0', 16], // RFC1918
    ['198.18.0.0', 15], // benchmarking
    ['198.51.100.0', 24], // documentation (TEST-NET-2)
    ['203.0.113.0', 24], // documentation (TEST-NET-3)
    ['224.0.0.0', 4], // multicast
    ['240.0.0.0', 4], // reserved + broadcast
  ];
  for (const [addr, prefix] of v4) list.addSubnet(addr, prefix, 'ipv4');
  const v6: Array<[string, number]> = [
    ['::', 96], // unspecified, loopback and the deprecated IPv4-compatible form (::7f00:1)
    ['::ffff:0:0:0', 96], // IPv4-translated (SIIT): embeds an IPv4 address
    ['64:ff9b::', 96], // NAT64: embeds an IPv4 address
    ['64:ff9b:1::', 48], // local-use NAT64: embeds an IPv4 address
    ['100::', 64], // discard-only
    ['2001::', 32], // Teredo: embeds an IPv4 address
    ['2001:db8::', 32], // documentation
    ['2002::', 16], // 6to4: embeds an IPv4 address
    ['fc00::', 7], // unique-local
    ['fe80::', 10], // link-local
    ['fec0::', 10], // site-local (deprecated, still routed on some networks)
    ['ff00::', 8], // multicast
  ];
  for (const [addr, prefix] of v6) list.addSubnet(addr, prefix, 'ipv6');
  return list;
})();

/**
 * Syntactic SSRF guard. Rejects hostnames that ARE (textually) private,
 * loopback, link-local, or cloud-metadata addresses. Layered with
 * `assertResolvedHostIsPublic` (DNS-resolution check) for defense in
 * depth against rebinding attacks where a public hostname resolves
 * to a private IP at fetch time.
 *
 * Note that `new URL('http://[::1]/').hostname` is "[::1]" WITH the brackets;
 * they are stripped here. IPv4 literals in decimal, hex or octal form are
 * normalised to dotted-quad by the URL parser before they reach this function.
 */
export function isPrivateOrLoopbackHost(hostname: string): boolean {
  if (!hostname) return true;
  let h = hostname.toLowerCase();
  if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1);
  // Zone ids (fe80::1%en0) are not part of the address.
  const zone = h.indexOf('%');
  if (zone !== -1) h = h.slice(0, zone);
  // "localhost." is the same name as "localhost" (a fully qualified form).
  if (h.endsWith('.')) h = h.slice(0, -1);
  if (!h) return true;
  // Hostname literals.
  if (
    h === 'localhost' ||
    h.endsWith('.localhost') ||
    h.endsWith('.local') ||
    h.endsWith('.internal') ||
    h.endsWith('.lan')
  ) {
    return true;
  }
  const family = net.isIP(h);
  if (family === 4) return PRIVATE_ADDRESS_RANGES.check(h, 'ipv4');
  if (family === 6) return PRIVATE_ADDRESS_RANGES.check(h, 'ipv6');
  // A name, not an address. Names that merely START with "fc", "fd" or "fe8"
  // (fcc.gov, fdic.gov) are ordinary public hosts; DNS resolution is checked
  // separately by assertResolvedHostIsPublic.
  return false;
}

/**
 * Default DNS resolver used when CrawlOptions.dnsLookup is omitted.
 * Returns ALL addresses for the hostname so the caller can reject
 * when ANY resolved IP is private (a multi-record DNS reply that mixes
 * a public IP with a private one still gets caught).
 */
const defaultDnsLookup: DnsLookupFn = (hostname) =>
  dnsPromises.lookup(hostname, { all: true });

/**
 * Returns true for hostnames that are already IP literals (and therefore
 * fully validated by isPrivateOrLoopbackHost without needing DNS).
 */
function isIpLiteral(hostname: string): boolean {
  const bare = hostname.startsWith('[') && hostname.endsWith(']') ? hostname.slice(1, -1) : hostname;
  return net.isIP(bare) !== 0;
}

type ResolvedAddress = { address: string; family: number };

/**
 * Resolve `hostname` through `lookup` and return its addresses only if NONE
 * of them is blocked. Shared by the pre-flight check
 * (`assertResolvedHostIsPublic`) and by the connection lookup hook
 * (`createGuardedLookup`), so both apply exactly the same rule.
 *
 * Fails closed: a DNS error, an empty answer, a malformed entry, or a single
 * blocked address in the answer set is a rejection. Rejecting on ANY blocked
 * address (not just when all are) matters: a reply that mixes a public and a
 * private record would otherwise leave the choice to the connection logic.
 */
async function resolveAllowedAddresses(
  hostname: string,
  lookup: DnsLookupFn,
  isBlocked: (hostOrAddress: string) => boolean
): Promise<{ ok: true; addresses: ResolvedAddress[] } | { ok: false; reason: string }> {
  if (!hostname) return { ok: false, reason: 'empty hostname' };
  if (isBlocked(hostname)) {
    return { ok: false, reason: `syntactically private host: ${hostname}` };
  }
  // A literal address needs no lookup (and some resolvers answer ENOTFOUND
  // for one); the check above has already judged it.
  if (isIpLiteral(hostname)) {
    const bare = bareHost(hostname);
    return { ok: true, addresses: [{ address: bare, family: net.isIP(bare) }] };
  }

  let addresses: ResolvedAddress[];
  try {
    addresses = await lookup(hostname);
  } catch (err) {
    return {
      ok: false,
      reason: `DNS lookup failed: ${(err as Error).message || 'unknown error'}`,
    };
  }
  if (!Array.isArray(addresses) || addresses.length === 0) {
    return { ok: false, reason: 'no DNS records' };
  }
  const accepted: ResolvedAddress[] = [];
  for (const entry of addresses) {
    const addr = entry && typeof entry.address === 'string' ? entry.address : '';
    const family = net.isIP(addr);
    if (!addr || family === 0) {
      return { ok: false, reason: 'malformed DNS response' };
    }
    if (isBlocked(addr)) {
      return { ok: false, reason: `resolves to private address ${addr}` };
    }
    accepted.push({ address: addr, family });
  }
  return { ok: true, addresses: accepted };
}

/**
 * Pre-flight DNS check: resolves the hostname and verifies that none of the
 * returned addresses is private, loopback, link-local or otherwise
 * non-public. Layered on `isPrivateOrLoopbackHost`, which catches literal
 * forms before any DNS query.
 *
 * On its own this check has a time-of-check/time-of-use gap: a hostile DNS
 * server can answer this query with a public address and the connection's
 * query, a moment later, with a private one. That gap is closed by the
 * transport, not here: `createPinnedFetch` runs the same validation inside
 * the socket's lookup hook, so the address that was validated is the address
 * that is dialled. This function remains as the early, well-worded rejection
 * and as the guard that still applies when a caller injects its own
 * `fetchImpl`.
 */
export async function assertResolvedHostIsPublic(
  hostname: string,
  lookup: DnsLookupFn = defaultDnsLookup
): Promise<{ ok: true } | { ok: false; reason: string }> {
  const result = await resolveAllowedAddresses(hostname, lookup, isPrivateOrLoopbackHost);
  return result.ok ? { ok: true } : result;
}

function bareHost(hostname: string): string {
  return hostname.startsWith('[') && hostname.endsWith(']') ? hostname.slice(1, -1) : hostname;
}

// ============================================================
// Pinned-connection transport
// ============================================================

/**
 * A `dns.lookup`-shaped function for `http.request({ lookup })`. Node calls
 * it when it is about to open the socket and then connects to what it
 * returns, so validating here pins the connection to validated addresses.
 * Exported for tests.
 */
export function createGuardedLookup(
  dnsLookup: DnsLookupFn = defaultDnsLookup,
  isBlocked: (hostOrAddress: string) => boolean = isPrivateOrLoopbackHost
): net.LookupFunction {
  return (hostname, options, callback) => {
    const wantsAll = Boolean(options && options.all);
    const fail = (message: string): void => {
      const err: NodeJS.ErrnoException = new Error(message);
      err.code = 'ECRAWLBLOCKED';
      callback(err, wantsAll ? [] : '', undefined);
    };
    resolveAllowedAddresses(hostname, dnsLookup, isBlocked).then(
      (result) => {
        if (!result.ok) {
          fail(`DNS-rebinding guard rejected ${hostname}: ${result.reason}`);
          return;
        }
        const family = options ? options.family : undefined;
        const preferred =
          family === 4 || family === 6
            ? result.addresses.filter((entry) => entry.family === family)
            : result.addresses;
        const usable = preferred.length > 0 ? preferred : result.addresses;
        if (wantsAll) {
          callback(null, usable);
        } else {
          callback(null, usable[0].address, usable[0].family);
        }
      },
      (err: unknown) => fail(`DNS lookup failed: ${(err as Error).message || 'unknown error'}`)
    );
  };
}

export interface PinnedFetchOptions {
  /** Resolver used inside the connection's lookup hook. Default: dns.lookup, all addresses. */
  dnsLookup?: DnsLookupFn;
  /**
   * Address policy. Defaults to `isPrivateOrLoopbackHost`, and the crawler
   * never passes anything else. It is a parameter only so the transport's
   * mechanics (decompression, header and body limits, redirects left to the
   * caller) can be exercised against a loopback test server.
   */
  isBlockedAddress?: (hostOrAddress: string) => boolean;
}

/**
 * A minimal fetch-compatible GET built on node:http and node:https, used
 * instead of the global fetch because the global one resolves the hostname
 * itself, out of our sight, at connect time. Here:
 *
 *  - the socket's lookup is `createGuardedLookup`, so a name is validated
 *    and connected to in one step (no DNS rebinding window);
 *  - a literal address is checked directly (Node skips the lookup hook for
 *    literals), and the connected socket's peer address is checked once more;
 *  - redirects are never followed (the caller validates each hop);
 *  - every request uses a fresh connection (`agent: false`), so a socket
 *    opened for one validated name is never reused for another;
 *  - response headers larger than MAX_RESPONSE_HEADER_BYTES abort the
 *    request, and gzip, deflate and brotli bodies are decoded as a stream so
 *    the caller's byte cap applies to the decoded size;
 *  - for https, the certificate is verified against the hostname as usual
 *    (the pinned address only decides where the TCP connection goes).
 *
 * Only what the crawler needs is implemented: GET, request headers, and an
 * AbortSignal. Exported for tests.
 */
export function createPinnedFetch(options: PinnedFetchOptions = {}): typeof fetch {
  const isBlocked = options.isBlockedAddress ?? isPrivateOrLoopbackHost;
  const lookup = createGuardedLookup(options.dnsLookup ?? defaultDnsLookup, isBlocked);

  const pinnedFetch = (input: unknown, init?: RequestInit): Promise<Response> =>
    new Promise<Response>((resolve, reject) => {
      let target: URL;
      try {
        target = new URL(typeof input === 'string' ? input : String((input as { href?: string }).href ?? input));
      } catch {
        reject(new TypeError('pinned fetch: invalid URL'));
        return;
      }
      if (target.protocol !== 'http:' && target.protocol !== 'https:') {
        reject(new TypeError(`pinned fetch: unsupported protocol ${target.protocol}`));
        return;
      }
      const host = bareHost(target.hostname);
      if (isBlocked(host)) {
        reject(new Error(`private host blocked: ${target.hostname}`));
        return;
      }

      const headers: Record<string, string> = {};
      new Headers(init?.headers as ConstructorParameters<typeof Headers>[0]).forEach((value, name) => {
        headers[name] = value;
      });
      headers['accept-encoding'] = 'gzip, deflate, br';
      headers.connection = 'close';

      const transport = target.protocol === 'https:' ? https : http;
      const req = transport.request({
        protocol: target.protocol,
        hostname: host,
        port: effectivePort(target),
        path: `${target.pathname}${target.search}`,
        method: 'GET',
        headers,
        agent: false,
        lookup,
        maxHeaderSize: MAX_RESPONSE_HEADER_BYTES,
        signal: init?.signal ?? undefined,
      });

      // Last assertion: whatever path led here, the peer we are actually
      // connected to must be an allowed address.
      req.once('socket', (socket) => {
        const verifyPeer = (): void => {
          const peer = socket.remoteAddress;
          if (!peer || isBlocked(peer)) {
            req.destroy(new Error(`connected address is not public: ${peer ?? 'unknown'}`));
          }
        };
        if (socket.connecting) socket.once('connect', verifyPeer);
        else verifyPeer();
      });
      req.once('error', (err) => reject(err));
      req.once('response', (incoming) => {
        try {
          resolve(toFetchResponse(incoming, req));
        } catch (err) {
          incoming.destroy();
          reject(err as Error);
        }
      });
      req.end();
    });

  return pinnedFetch as typeof fetch;
}

/** Statuses whose responses carry no body (a Response refuses one for them). */
const NULL_BODY_STATUSES: ReadonlySet<number> = new Set([204, 205, 304]);

function toFetchResponse(incoming: http.IncomingMessage, req: http.ClientRequest): Response {
  const status = incoming.statusCode ?? 0;
  if (status < 200 || status > 599) {
    throw new Error(`HTTP ${status}`);
  }

  const headers = new Headers();
  for (let i = 0; i + 1 < incoming.rawHeaders.length; i += 2) {
    try {
      headers.append(incoming.rawHeaders[i], incoming.rawHeaders[i + 1]);
    } catch {
      // A header the WHATWG Headers class refuses (bad name or value) is
      // dropped; the crawler only reads location and content-type.
    }
  }

  if (NULL_BODY_STATUSES.has(status)) {
    incoming.destroy();
    return new Response(null, { status, headers });
  }

  let body: Readable = incoming;
  const encoding = String(incoming.headers['content-encoding'] || '').trim().toLowerCase();
  const decoder =
    encoding === 'gzip' || encoding === 'x-gzip'
      ? zlib.createGunzip()
      : encoding === 'deflate'
        ? zlib.createInflate()
        : encoding === 'br'
          ? zlib.createBrotliDecompress()
          : null;
  if (decoder) {
    // pipeline() destroys both streams if either fails or closes early, so a
    // cancelled read also tears down the socket. The error itself reaches the
    // reader through the decoder; the callback only has to exist.
    body = pipeline(incoming, decoder, () => undefined);
    headers.delete('content-encoding');
    headers.delete('content-length');
  } else if (encoding && encoding !== 'identity') {
    throw new Error(`unsupported content-encoding: ${encoding.slice(0, 40)}`);
  }
  body.once('close', () => req.destroy());

  return new Response(Readable.toWeb(body) as unknown as ConstructorParameters<typeof Response>[0], {
    status,
    headers,
  });
}

function clampInt(
  raw: unknown,
  lo: number,
  hi: number,
  fallback: number
): number {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return fallback;
  const n = Math.floor(raw);
  if (n < lo) return lo;
  if (n > hi) return hi;
  return n;
}
