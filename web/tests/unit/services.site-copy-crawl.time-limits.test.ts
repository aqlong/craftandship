import { describe, it, expect } from 'vitest';
import { crawlSite, type DnsLookupFn } from '../../src/services/site_copy_crawl';

/**
 * The crawler documents a 15-second limit per request and a 60-second
 * deadline for a crawl. These tests hold it to both when a step simply never
 * finishes: a DNS lookup (which cannot be cancelled), a transport that
 * ignores the abort signal, a body that stops arriving. Each case uses the
 * smallest limit the crawler accepts (1 s per request, 5 s per crawl) and
 * checks that `crawlSite` came back on time with a warning that says why.
 *
 * Only `crawlSite` is used, so the file can be run unchanged against an
 * older version of the module, where these cases wait forever.
 */

const PUBLIC_DNS: DnsLookupFn = async () => [{ address: '93.184.216.34', family: 4 }];

const home = '<html><body><main><h1>Home</h1><p>Welcome in.</p></main></body></html>';

function htmlResponse(body: string): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'text/html' }),
    text: async () => body,
  } as unknown as Response;
}

/** Runs `work` and reports how long it took. */
async function timed<T>(work: () => Promise<T>): Promise<{ outcome: T; elapsedMs: number }> {
  const started = performance.now();
  const outcome = await work();
  return { outcome, elapsedMs: performance.now() - started };
}

describe('crawlSite time limits on the fetching side', () => {
  it('control: with a resolver and a transport that answer, the same crawl succeeds', async () => {
    const result = await crawlSite('https://studio.example/', {
      fetchImpl: (async () => htmlResponse(home)) as unknown as typeof fetch,
      dnsLookup: PUBLIC_DNS,
      requestTimeoutMs: 1000,
      maxPages: 1,
    });
    expect(result.warnings).toEqual([]);
    expect(result.pages.map((p) => p.markdown_blocks)).toEqual([['# Home', 'Welcome in.']]);
  });

  it('a DNS lookup that never answers ends the crawl at the request timeout', async () => {
    let fetched = 0;
    const neverAnswers: DnsLookupFn = () => new Promise(() => undefined);
    const finished = await timed(() =>
      crawlSite('https://studio.example/', {
        fetchImpl: (async () => {
          fetched += 1;
          return htmlResponse(home);
        }) as unknown as typeof fetch,
        dnsLookup: neverAnswers,
        requestTimeoutMs: 1000,
      })
    );
    expect(finished.outcome.pages).toEqual([]);
    expect(finished.outcome.warnings).toEqual([
      'DNS-rebinding guard rejected studio.example: DNS lookup timed out after 1000ms',
    ]);
    expect(fetched).toBe(0);
    expect(finished.elapsedMs).toBeGreaterThanOrEqual(990);
    expect(finished.elapsedMs).toBeLessThan(4000);
  });

  it('a DNS lookup that hangs for a later request is cut off too, and nothing is sent for it', async () => {
    let lookups = 0;
    let fetched = 0;
    const secondNeverAnswers: DnsLookupFn = () => {
      lookups += 1;
      if (lookups === 1) return Promise.resolve([{ address: '93.184.216.34', family: 4 }]);
      return new Promise(() => undefined);
    };
    const finished = await timed(() =>
      crawlSite('https://studio.example/', {
        fetchImpl: (async () => {
          fetched += 1;
          return htmlResponse(home);
        }) as unknown as typeof fetch,
        dnsLookup: secondNeverAnswers,
        requestTimeoutMs: 1000,
        maxPages: 1,
      })
    );
    expect(finished.outcome.pages).toEqual([]);
    expect(finished.outcome.warnings).toEqual(['fetch https://studio.example/ failed: timed out after 1000ms']);
    expect(fetched).toBe(0);
    expect(finished.elapsedMs).toBeLessThan(4000);
  });

  it('a transport that ignores the abort signal and never answers is abandoned at the request timeout', async () => {
    let aborted = false;
    const fetchImpl = ((_url: string | URL, init?: RequestInit) => {
      init?.signal?.addEventListener('abort', () => {
        aborted = true;
      });
      return new Promise<Response>(() => undefined);
    }) as unknown as typeof fetch;
    const finished = await timed(() =>
      crawlSite('https://studio.example/', {
        fetchImpl,
        dnsLookup: PUBLIC_DNS,
        requestTimeoutMs: 1000,
        maxPages: 1,
      })
    );
    expect(finished.outcome.warnings).toEqual(['fetch https://studio.example/ failed: timed out after 1000ms']);
    // The signal was raised as well, for transports that do honour it.
    expect(aborted).toBe(true);
    expect(finished.elapsedMs).toBeGreaterThanOrEqual(990);
    expect(finished.elapsedMs).toBeLessThan(4000);
  });

  it('a body that stops arriving is abandoned at the request timeout', async () => {
    const stalled = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('<h1>Half a pa'));
        // Never closes, never enqueues again.
      },
    });
    const fetchImpl = (async () =>
      new Response(stalled, { status: 200, headers: { 'content-type': 'text/html' } })) as unknown as typeof fetch;
    const finished = await timed(() =>
      crawlSite('https://studio.example/', {
        fetchImpl,
        dnsLookup: PUBLIC_DNS,
        requestTimeoutMs: 1000,
        maxPages: 1,
      })
    );
    expect(finished.outcome.pages).toEqual([]);
    expect(finished.outcome.warnings).toEqual(['fetch https://studio.example/ failed: timed out after 1000ms']);
    expect(finished.elapsedMs).toBeLessThan(4000);
  });

  it('the crawl deadline cuts a request short even when the request timeout would allow longer', async () => {
    let calls = 0;
    const fetchImpl = (() => {
      calls += 1;
      return new Promise<Response>(() => undefined);
    }) as unknown as typeof fetch;
    const finished = await timed(() =>
      crawlSite('https://studio.example/', {
        fetchImpl,
        dnsLookup: PUBLIC_DNS,
        requestTimeoutMs: 60_000,
        totalTimeoutMs: 5000, // the smallest deadline the crawler accepts
      })
    );
    expect(calls).toBe(1);
    expect(finished.outcome.pages).toEqual([]);
    expect(finished.outcome.warnings).toHaveLength(1);
    expect(finished.outcome.warnings[0]).toMatch(/^fetch https:\/\/studio\.example\/ failed: timed out after (49\d\d|5000)ms$/);
    expect(finished.elapsedMs).toBeGreaterThanOrEqual(4900);
    expect(finished.elapsedMs).toBeLessThan(8000);
  });
});
