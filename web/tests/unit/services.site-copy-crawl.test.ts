import { describe, it, expect, afterEach } from 'vitest';
import http from 'http';
import zlib from 'zlib';
import type { AddressInfo } from 'net';
import {
  crawlSite,
  segmentHtmlToCopyPage,
  extractInternalLinks,
  normalizePath,
  isPrivateOrLoopbackHost,
  assertResolvedHostIsPublic,
  crawlTargetProblem,
  isSameSiteHost,
  createGuardedLookup,
  createPinnedFetch,
  type DnsLookupFn,
} from '../../src/services/site_copy_crawl';

// Canned DNS lookup that returns a public IP for any hostname. Most
// fixture hostnames in this file (studio.example, x.example, etc.) do
// not actually resolve, so without an injected lookup the DNS-rebinding
// guard would reject every crawl. Tests targeting the rebinding guard
// itself pass a different lookup that returns a private IP.
const PUBLIC_DNS: DnsLookupFn = async () => [
  { address: '93.184.216.34', family: 4 },
];

// Em-dash unicode escape so the lint scanner sees no literal U+2014 in
// source. The runtime fixtures still contain real em dashes (the
// faithful-copy carve-out is precisely about preserving them verbatim
// from customer-imported text).
const EM = String.fromCharCode(0x2014);

/**
 * Tests for the faithful-copy crawler (v2-faithful-site-copy-mode).
 *
 * The crawler is a pure-fetch HTML segmenter. We don't hit the network
 * here; every test injects `fetchImpl` with canned HTML. Network +
 * timeout behavior is tested via the contract on `fetchImpl` (returns
 * a Response-shaped object).
 *
 * Brand-voice em-dash carve-out: any test fixture below that contains
 * em dashes represents customer-imported text. Em dashes in those
 * fixtures must be preserved verbatim.
 */

// --- A minimal Response-like used by injected fetch mocks. ---
function fakeResponse(body: string, contentType = 'text/html'): Response {
  // brand-voice-allow-em-dash: helper for testing customer copy with em dashes
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': contentType }),
    text: async () => body,
  } as unknown as Response;
}

function makeFetch(map: Record<string, string>) {
  return async (url: string | URL): Promise<Response> => {
    const u = typeof url === 'string' ? url : url.toString();
    const body = map[u];
    if (body == null) {
      return {
        ok: false,
        status: 404,
        headers: new Headers(),
        text: async () => '',
      } as unknown as Response;
    }
    return fakeResponse(body);
  };
}

// ============================================================
// segmentHtmlToCopyPage
// ============================================================

describe('segmentHtmlToCopyPage', () => {
  it('extracts headings + paragraphs + list items as markdown blocks', () => {
    const html = `
      <html><head><title>Sample Practice</title></head>
      <body>
        <nav><a href="/">home</a></nav>
        <main>
          <h1>Sample Practice</h1>
          <p>Sample one, sample two, sample three.</p>
          <h2>What I offer</h2>
          <ul>
            <li>Initial session</li>
            <li>Follow-up</li>
          </ul>
          <blockquote>"A gentle, grounded space." ${EM} A.</blockquote>
        </main>
        <footer>boilerplate</footer>
      </body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp).not.toBeNull();
    expect(cp!.title).toBe('Sample Practice');
    expect(cp!.markdown_blocks).toContain('# Sample Practice');
    expect(cp!.markdown_blocks).toContain('Sample one, sample two, sample three.');
    expect(cp!.markdown_blocks).toContain('## What I offer');
    expect(cp!.markdown_blocks).toContain('- Initial session');
    expect(cp!.markdown_blocks).toContain('- Follow-up');
    expect(cp!.markdown_blocks.some((b) => b.startsWith('> '))).toBe(true);
  });

  it('strips script + style + nav + footer from output', () => {
    const html = `
      <html><body>
        <script>alert('nope')</script>
        <style>body{display:none}</style>
        <nav><p>nav text should not appear</p></nav>
        <main><p>visible content</p></main>
        <footer><p>footer text should not appear</p></footer>
      </body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp).not.toBeNull();
    expect(cp!.markdown_blocks).toContain('visible content');
    const all = cp!.markdown_blocks.join(' | ');
    expect(all).not.toContain('alert');
    expect(all).not.toContain('display:none');
    expect(all).not.toContain('nav text');
    expect(all).not.toContain('footer text');
  });

  it('preserves em dashes in customer copy verbatim', () => {
    const html = `
      <html><body><main>
        <p>Sample alpha${EM}sample beta.</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.markdown_blocks[0]).toBe('Sample alpha' + EM + 'sample beta.');
  });

  it('decodes HTML entities (mdash, nbsp, amp)', () => {
    const html = `
      <html><body><main>
        <p>Chai &amp; tea&mdash;always.&nbsp;Always.</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.markdown_blocks[0]).toBe('Chai & tea' + EM + 'always. Always.');
  });

  it('renders inline bold + italic + link as markdown', () => {
    const html = `
      <html><body><main>
        <p>This is <strong>bold</strong> and <em>italic</em> with a <a href="/about">link</a>.</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.markdown_blocks[0]).toBe(
      'This is **bold** and *italic* with a [link](/about).'
    );
  });

  it('drops javascript: and data: hrefs from links', () => {
    const html = `
      <html><body><main>
        <p>Bad <a href="javascript:alert(1)">click</a> link.</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    // The link text is kept; the href is stripped.
    expect(cp!.markdown_blocks[0]).toBe('Bad click link.');
    expect(cp!.markdown_blocks[0]).not.toContain('javascript:');
  });

  it('detects hero from first H1 + following paragraphs', () => {
    const html = `
      <html><body><main>
        <h1>Sample Studio</h1>
        <p>Sample tagline one, sample tagline two, sample tagline three.</p>
        <p>Sessions in person and online.</p>
        <h2>Offerings</h2>
        <p>Other content past the hero.</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.hero?.heading).toBe('Sample Studio');
    expect(cp!.hero?.body_html).toContain('Sample tagline one');
    expect(cp!.hero?.body_html).toContain('Sessions in person');
    // Cap at 2 paragraphs.
    expect(cp!.hero?.body_html).not.toContain('Other content past the hero');
  });

  it('detects sms link as cta_text_me_phone (digits only)', () => {
    const html = `
      <html><body>
        <a href="sms:+1-555-555-0112" class="cta">TEXT ME</a>
        <main><h1>Me</h1><p>hi.</p></main>
      </body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.hero?.cta_text_me_phone).toBe('15555550112');
  });

  it('returns null when HTML has no extractable content', () => {
    expect(segmentHtmlToCopyPage('/', '')).toBeNull();
    expect(
      segmentHtmlToCopyPage('/', '<html><body></body></html>')
    ).toBeNull();
  });

  it('caps blocks at 60 per page', () => {
    const paras = Array.from({ length: 200 }, (_v, i) => `<p>Item ${i}.</p>`).join(
      '\n'
    );
    const cp = segmentHtmlToCopyPage(
      '/',
      `<html><body><main>${paras}</main></body></html>`
    );
    expect(cp!.markdown_blocks.length).toBeLessThanOrEqual(60);
  });
});

// ============================================================
// extractInternalLinks
// ============================================================

describe('extractInternalLinks', () => {
  it('returns same-origin paths from anchors', () => {
    const html = `
      <a href="/about">about</a>
      <a href="/services">services</a>
      <a href="https://example.com/contact">contact (absolute same origin)</a>
      <a href="https://other.example/elsewhere">external</a>
      <a href="mailto:a@example.test">email</a>
      <a href="#hash">hash</a>
    `;
    const out = extractInternalLinks(html, 'https://example.com');
    expect(out).toContain('/about');
    expect(out).toContain('/services');
    expect(out).toContain('/contact');
    expect(out).not.toContain('/elsewhere');
  });

  it('skips asset paths + auth surfaces', () => {
    const html = `
      <a href="/about">about</a>
      <a href="/wp-admin">wp-admin</a>
      <a href="/login">login</a>
      <a href="/static/photo.jpg">photo</a>
      <a href="/styles.css">styles</a>
    `;
    const out = extractInternalLinks(html, 'https://example.com');
    expect(out).toEqual(['/about']);
  });

  it('does not include the home path', () => {
    const html = `
      <a href="/">home</a>
      <a href="/about">about</a>
    `;
    const out = extractInternalLinks(html, 'https://example.com');
    expect(out).toEqual(['/about']);
  });

  it('returns alphabetically sorted output for determinism', () => {
    const html = `
      <a href="/zebra">z</a>
      <a href="/apple">a</a>
      <a href="/mango">m</a>
    `;
    const out = extractInternalLinks(html, 'https://example.com');
    expect(out).toEqual(['/apple', '/mango', '/zebra']);
  });
});

// ============================================================
// normalizePath
// ============================================================

describe('normalizePath', () => {
  it('lowercases + strips trailing slash', () => {
    expect(normalizePath('/About/')).toBe('/about');
    expect(normalizePath('/Services/Appliances/')).toBe('/services/appliances');
  });

  it('preserves root', () => {
    expect(normalizePath('/')).toBe('/');
  });

  it('strips query + hash', () => {
    expect(normalizePath('/foo?x=1')).toBe('/foo');
    expect(normalizePath('/foo#bar')).toBe('/foo');
  });

  it('coerces a missing leading slash', () => {
    expect(normalizePath('about')).toBe('/about');
  });
});

// ============================================================
// crawlSite (end-to-end with injected fetch)
// ============================================================

describe('crawlSite', () => {
  it('crawls home + linked pages and returns deterministic output', async () => {
    const home = `
      <html><head><title>Sample & Studio</title></head>
      <body>
        <nav>
          <a href="/about">About</a>
          <a href="/services">Sessions</a>
          <a href="https://other.example/x">External</a>
        </nav>
        <main>
          <h1>Sample &amp; Studio</h1>
          <p>Sessions by appointment.</p>
        </main>
      </body></html>
    `;
    const about = `
      <html><body><main>
        <h1>About</h1>
        <p>I have practiced for many years.</p>
      </main></body></html>
    `;
    const services = `
      <html><body><main>
        <h1>Sessions</h1>
        <ul><li>Initial</li><li>Follow-up</li></ul>
      </main></body></html>
    `;
    const fetchImpl = makeFetch({
      'https://studio.example/': home,
      'https://studio.example/about': about,
      'https://studio.example/services': services,
    });
    const result = await crawlSite('https://studio.example/', { fetchImpl, dnsLookup: PUBLIC_DNS });
    expect(result.warnings).toEqual([]);
    expect(result.pages.length).toBe(3);
    // Home first.
    expect(result.pages[0].path).toBe('/');
    expect(result.pages[0].title).toBe('Sample & Studio');
    // Remaining pages alphabetically.
    expect(result.pages[1].path).toBe('/about');
    expect(result.pages[2].path).toBe('/services');
    // Same-input → same output.
    const replay = await crawlSite('https://studio.example/', { fetchImpl, dnsLookup: PUBLIC_DNS });
    expect(JSON.stringify(replay)).toBe(JSON.stringify(result));
  });

  it('honors maxPages + does not exceed it', async () => {
    const home = `
      <html><body>
        <main>
          <a href="/p1">p1</a>
          <a href="/p2">p2</a>
          <a href="/p3">p3</a>
          <a href="/p4">p4</a>
          <a href="/p5">p5</a>
          <h1>Home</h1>
          <p>here.</p>
        </main>
      </body></html>
    `;
    const map: Record<string, string> = {
      'https://x.example/': home,
    };
    for (const p of ['/p1', '/p2', '/p3', '/p4', '/p5']) {
      map[`https://x.example${p}`] = `<html><body><main><h1>${p}</h1><p>x</p></main></body></html>`;
    }
    const fetchImpl = makeFetch(map);
    const result = await crawlSite('https://x.example/', {
      fetchImpl,
      maxPages: 3,
      dnsLookup: PUBLIC_DNS,
    });
    expect(result.pages.length).toBe(3);
  });

  it('returns warning when start URL is not http(s)', async () => {
    const result = await crawlSite('javascript:alert(1)', {
      fetchImpl: makeFetch({}),
      dnsLookup: PUBLIC_DNS,
    });
    expect(result.pages).toEqual([]);
    expect(result.warnings.some((w) => /http\(s\)/.test(w))).toBe(true);
  });

  it('captures fetch failures as warnings, not throws', async () => {
    const fetchImpl = async () => {
      throw new Error('ECONNREFUSED');
    };
    const result = await crawlSite('https://broken.example/', {
      fetchImpl,
      dnsLookup: PUBLIC_DNS,
    });
    expect(result.pages).toEqual([]);
    expect(
      result.warnings.some((w) => /ECONNREFUSED|fetch/i.test(w))
    ).toBe(true);
  });

  it('preserves customer em dashes through the crawl pipeline', async () => {
    const html = `
      <html><body><main>
        <h1>Studio</h1>
        <p>Sample alpha${EM}sample beta${EM}sample gamma.</p>
      </main></body></html>
    `;
    const fetchImpl = makeFetch({ 'https://s.example/': html });
    const result = await crawlSite('https://s.example/', {
      fetchImpl,
      dnsLookup: PUBLIC_DNS,
    });
    expect(result.pages[0].markdown_blocks).toContain(
      'Sample alpha' + EM + 'sample beta' + EM + 'sample gamma.'
    );
  });
});

// ============================================================
// SSRF guard
// ============================================================

describe('isPrivateOrLoopbackHost', () => {
  it('rejects localhost variants', () => {
    expect(isPrivateOrLoopbackHost('localhost')).toBe(true);
    expect(isPrivateOrLoopbackHost('foo.localhost')).toBe(true);
    expect(isPrivateOrLoopbackHost('printer.local')).toBe(true);
    expect(isPrivateOrLoopbackHost('intranet.lan')).toBe(true);
    expect(isPrivateOrLoopbackHost('docs.internal')).toBe(true);
  });

  it('rejects RFC1918 + cloud-metadata + loopback IPv4', () => {
    expect(isPrivateOrLoopbackHost('127.0.0.1')).toBe(true);
    expect(isPrivateOrLoopbackHost('127.255.0.1')).toBe(true);
    expect(isPrivateOrLoopbackHost('10.0.0.1')).toBe(true);
    expect(isPrivateOrLoopbackHost('10.255.255.255')).toBe(true);
    expect(isPrivateOrLoopbackHost('172.16.0.1')).toBe(true);
    expect(isPrivateOrLoopbackHost('172.31.255.255')).toBe(true);
    expect(isPrivateOrLoopbackHost('192.168.0.1')).toBe(true);
    expect(isPrivateOrLoopbackHost('169.254.169.254')).toBe(true); // AWS metadata
    expect(isPrivateOrLoopbackHost('0.0.0.0')).toBe(true);
    expect(isPrivateOrLoopbackHost('224.0.0.1')).toBe(true); // multicast
  });

  it('rejects loopback + link-local + ULA IPv6', () => {
    expect(isPrivateOrLoopbackHost('::1')).toBe(true);
    expect(isPrivateOrLoopbackHost('0:0:0:0:0:0:0:1')).toBe(true);
    expect(isPrivateOrLoopbackHost('fe80::1')).toBe(true);
    expect(isPrivateOrLoopbackHost('fc00::1')).toBe(true);
    expect(isPrivateOrLoopbackHost('fd12:3456:789a::1')).toBe(true);
  });

  it('accepts public hosts + IPs', () => {
    expect(isPrivateOrLoopbackHost('craftandship.io')).toBe(false);
    expect(isPrivateOrLoopbackHost('example.com')).toBe(false);
    expect(isPrivateOrLoopbackHost('subdomain.example.com')).toBe(false);
    expect(isPrivateOrLoopbackHost('8.8.8.8')).toBe(false);
    expect(isPrivateOrLoopbackHost('172.15.0.1')).toBe(false); // just outside RFC1918
    expect(isPrivateOrLoopbackHost('172.32.0.1')).toBe(false); // just outside RFC1918
    expect(isPrivateOrLoopbackHost('169.253.0.1')).toBe(false); // just outside link-local
  });

  it('rejects empty hostname defensively', () => {
    expect(isPrivateOrLoopbackHost('')).toBe(true);
  });
});

describe('isPrivateOrLoopbackHost: IPv6 literals as the URL parser reports them', () => {
  // new URL('http://[::1]/').hostname is "[::1]" WITH brackets. The guard used
  // to compare against "::1" and so let every bracketed IPv6 literal through.
  it('URL.hostname really does keep the brackets (the premise of this block)', () => {
    expect(new URL('http://[::1]/').hostname).toBe('[::1]');
  });

  it('rejects bracketed loopback, unspecified, link-local and unique-local literals', () => {
    for (const host of ['[::1]', '[::]', '[fe80::1]', '[feb0::1]', '[fc00::1]', '[fd12:3456:789a::1]']) {
      expect(isPrivateOrLoopbackHost(host), host).toBe(true);
    }
  });

  it('rejects IPv4-mapped IPv6 addresses that point at private IPv4 space', () => {
    // Hostnames as the URL parser normalises them, and raw resolver output.
    for (const host of ['[::ffff:7f00:1]', '[::ffff:a00:1]', '::ffff:127.0.0.1', '::ffff:10.0.0.1', '::ffff:169.254.169.254']) {
      expect(isPrivateOrLoopbackHost(host), host).toBe(true);
    }
    expect(isPrivateOrLoopbackHost('::ffff:8.8.8.8')).toBe(false);
  });

  it('rejects carrier-grade NAT and other non-public IPv4 ranges', () => {
    for (const host of ['100.64.0.1', '100.127.255.255', '198.18.0.1', '240.0.0.1', '192.0.0.8']) {
      expect(isPrivateOrLoopbackHost(host), host).toBe(true);
    }
    expect(isPrivateOrLoopbackHost('100.128.0.1')).toBe(false);
  });

  it('accepts a public IPv6 literal', () => {
    expect(isPrivateOrLoopbackHost('[2606:4700:4700::1111]')).toBe(false);
  });

  it('does not treat ordinary names that start with fc, fd or fe8 as IPv6 ranges', () => {
    for (const host of ['fcc.gov', 'fda.gov', 'fdic.gov', 'fcbarcelona.com', 'fe80s.example', 'fe9.example.org']) {
      expect(isPrivateOrLoopbackHost(host), host).toBe(false);
    }
  });
});

describe('crawlSite SSRF guard (IPv6 literal start URLs)', () => {
  it('refuses http://[::1]:4000/ without issuing any request', async () => {
    let fetched = 0;
    const result = await crawlSite('http://[::1]:4000/admin', {
      fetchImpl: async () => {
        fetched += 1;
        return fakeResponse('<h1>internal</h1>');
      },
      dnsLookup: PUBLIC_DNS,
    });
    expect(result.pages).toEqual([]);
    expect(fetched).toBe(0);
  });

  it('refuses an IPv4-mapped metadata address written as an IPv6 literal', async () => {
    let fetched = 0;
    const result = await crawlSite('http://[::ffff:169.254.169.254]/latest/meta-data/', {
      fetchImpl: async () => {
        fetched += 1;
        return fakeResponse('x');
      },
      dnsLookup: PUBLIC_DNS,
    });
    expect(result.pages).toEqual([]);
    expect(fetched).toBe(0);
  });

  it('rejects a redirect to a bracketed IPv6 loopback literal', async () => {
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u === 'https://public.example/') {
        return {
          ok: false,
          status: 302,
          headers: new Headers({ location: 'http://[::1]:4000/' }),
          text: async () => '',
        } as unknown as Response;
      }
      return fakeResponse('<h1>secret</h1>');
    };
    const result = await crawlSite('https://public.example/', { fetchImpl, dnsLookup: PUBLIC_DNS });
    expect(result.pages).toEqual([]);
    expect(result.warnings.some((w) => /private host/i.test(w))).toBe(true);
  });

  it('assertResolvedHostIsPublic rejects a DNS answer that is an IPv4-mapped private address', async () => {
    const mapped: DnsLookupFn = async () => [{ address: '::ffff:10.0.0.5', family: 6 }];
    const verdict = await assertResolvedHostIsPublic('rebind.example', mapped);
    expect(verdict.ok).toBe(false);
  });
});

describe('crawlSite response handling', () => {
  it('stops reading a streaming body once the byte cap is reached', async () => {
    let chunksPulled = 0;
    const chunk = new TextEncoder().encode('<p>' + 'x'.repeat(1000) + '</p>');
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        chunksPulled += 1;
        controller.enqueue(chunk);
        if (chunksPulled > 100_000) controller.close(); // would be ~100 MB if never cancelled
      },
    });
    const fetchImpl = async (): Promise<Response> =>
      new Response(stream, { status: 200, headers: { 'content-type': 'text/html' } });

    const result = await crawlSite('https://big.example/', {
      fetchImpl,
      dnsLookup: PUBLIC_DNS,
      maxBodyBytes: 4096,
      maxPages: 1,
    });

    // 4096 bytes is five chunks; allow generous slack for stream read-ahead,
    // but nowhere near the 100,000 a full buffer would pull.
    expect(chunksPulled).toBeLessThan(50);
    expect(result.pages.length).toBe(1);
  });

  it('does not abort the crawl on an out-of-range numeric character reference', () => {
    const html = '<html><body><p>Open &#x110000; and &#99999999999; daily &amp; weekly</p></body></html>';
    let page: ReturnType<typeof segmentHtmlToCopyPage> = null;
    expect(() => {
      page = segmentHtmlToCopyPage('/', html);
    }).not.toThrow();
    expect(page).not.toBeNull();
    expect(page!.markdown_blocks[0]).toContain('daily & weekly');
  });
});

describe('crawlSite SSRF guard', () => {
  // The injected transport answers 404 to anything, so "no pages came back"
  // would also be true with no guard at all. What proves the guard is that the
  // transport is never asked for the address and the warning names the reason.
  async function crawlCountingFetches(startUrl: string) {
    const requested: string[] = [];
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      requested.push(String(url));
      return makeFetch({})(url);
    };
    const result = await crawlSite(startUrl, { fetchImpl, dnsLookup: PUBLIC_DNS });
    return { result, requested };
  }

  it.each([
    ['localhost by address', 'http://127.0.0.1/'],
    ['the AWS metadata host', 'http://169.254.169.254/latest/meta-data/'],
    ['an RFC1918 address', 'http://10.0.0.1/'],
  ])('rejects a start URL on %s without issuing a request', async (_label, startUrl) => {
    const { result, requested } = await crawlCountingFetches(startUrl);
    expect(result.pages).toEqual([]);
    expect(requested).toEqual([]);
    expect(result.warnings.some((warning) => /private host blocked/i.test(warning))).toBe(true);
  });

  it('control: the same harness does request a public host (so the zero above means something)', async () => {
    const { requested } = await crawlCountingFetches('https://public.example/');
    expect(requested).toContain('https://public.example/');
  });

  it('rejects redirect to a private host as a fetch warning', async () => {
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u === 'https://public.example/') {
        return {
          ok: false,
          status: 302,
          headers: new Headers({ location: 'http://169.254.169.254/' }),
          text: async () => '',
        } as unknown as Response;
      }
      return {
        ok: false,
        status: 404,
        headers: new Headers(),
        text: async () => '',
      } as unknown as Response;
    };
    const result = await crawlSite('https://public.example/', {
      fetchImpl,
      dnsLookup: PUBLIC_DNS,
    });
    expect(result.pages).toEqual([]);
    expect(
      result.warnings.some((w) => /private host/i.test(w) || /redirect/i.test(w))
    ).toBe(true);
  });
});

// ============================================================
// DNS-rebinding guard (Phase 1 hardening follow-up)
// ============================================================

describe('assertResolvedHostIsPublic', () => {
  it('returns ok for a public name resolving to a public IP', async () => {
    const lookup: DnsLookupFn = async () => [
      { address: '93.184.216.34', family: 4 },
    ];
    const result = await assertResolvedHostIsPublic('example.com', lookup);
    expect(result.ok).toBe(true);
  });

  it('rejects a public name that resolves to AWS metadata 169.254.169.254', async () => {
    const lookup: DnsLookupFn = async () => [
      { address: '169.254.169.254', family: 4 },
    ];
    const result = await assertResolvedHostIsPublic('rebind.example', lookup);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toMatch(/169\.254\.169\.254/);
    }
  });

  it('rejects a public name that resolves to RFC1918', async () => {
    const lookup: DnsLookupFn = async () => [
      { address: '10.0.0.5', family: 4 },
    ];
    const result = await assertResolvedHostIsPublic('rebind.example', lookup);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toMatch(/10\.0\.0\.5/);
    }
  });

  it('rejects when ANY resolved address is private (mixed reply)', async () => {
    const lookup: DnsLookupFn = async () => [
      { address: '8.8.8.8', family: 4 },
      { address: '127.0.0.1', family: 4 },
    ];
    const result = await assertResolvedHostIsPublic('mixed.example', lookup);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toMatch(/127\.0\.0\.1/);
    }
  });

  it('rejects a public name that resolves to IPv6 loopback', async () => {
    const lookup: DnsLookupFn = async () => [
      { address: '::1', family: 6 },
    ];
    const result = await assertResolvedHostIsPublic('rebind6.example', lookup);
    expect(result.ok).toBe(false);
  });

  it('rejects a public name that resolves to IPv6 link-local', async () => {
    const lookup: DnsLookupFn = async () => [
      { address: 'fe80::1234', family: 6 },
    ];
    const result = await assertResolvedHostIsPublic('rebind6.example', lookup);
    expect(result.ok).toBe(false);
  });

  it('rejects when DNS lookup throws (fail-closed)', async () => {
    const lookup: DnsLookupFn = async () => {
      throw new Error('ENOTFOUND');
    };
    const result = await assertResolvedHostIsPublic('missing.example', lookup);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toMatch(/ENOTFOUND|DNS/);
    }
  });

  it('rejects when DNS returns an empty address list', async () => {
    const lookup: DnsLookupFn = async () => [];
    const result = await assertResolvedHostIsPublic('empty.example', lookup);
    expect(result.ok).toBe(false);
  });

  it('rejects a syntactically private hostname without doing a lookup', async () => {
    let lookupCalled = false;
    const lookup: DnsLookupFn = async () => {
      lookupCalled = true;
      return [{ address: '93.184.216.34', family: 4 }];
    };
    const result = await assertResolvedHostIsPublic('printer.local', lookup);
    expect(result.ok).toBe(false);
    expect(lookupCalled).toBe(false);
  });

  it('short-circuits IP literals (already covered by syntactic check)', async () => {
    let lookupCalled = false;
    const lookup: DnsLookupFn = async () => {
      lookupCalled = true;
      return [];
    };
    const result = await assertResolvedHostIsPublic('8.8.8.8', lookup);
    expect(result.ok).toBe(true);
    expect(lookupCalled).toBe(false);
  });

  it('rejects empty hostname defensively', async () => {
    const result = await assertResolvedHostIsPublic('', PUBLIC_DNS);
    expect(result.ok).toBe(false);
  });
});

describe('crawlSite DNS-rebinding guard', () => {
  it('rejects a public start URL whose hostname resolves to AWS metadata', async () => {
    const rebindingLookup: DnsLookupFn = async () => [
      { address: '169.254.169.254', family: 4 },
    ];
    let fetchCalled = false;
    const fetchImpl = async () => {
      fetchCalled = true;
      return fakeResponse('<html></html>');
    };
    const result = await crawlSite('https://attacker.example/', {
      fetchImpl,
      dnsLookup: rebindingLookup,
    });
    expect(result.pages).toEqual([]);
    expect(fetchCalled).toBe(false);
    expect(
      result.warnings.some((w) =>
        /DNS-rebinding|169\.254\.169\.254|private/i.test(w)
      )
    ).toBe(true);
  });

  it('rejects a public start URL whose hostname resolves to RFC1918', async () => {
    const rebindingLookup: DnsLookupFn = async () => [
      { address: '10.0.0.1', family: 4 },
    ];
    let fetchCalled = false;
    const fetchImpl = async () => {
      fetchCalled = true;
      return fakeResponse('<html></html>');
    };
    const result = await crawlSite('https://attacker.example/', {
      fetchImpl,
      dnsLookup: rebindingLookup,
    });
    expect(result.pages).toEqual([]);
    expect(fetchCalled).toBe(false);
  });

  it('rejects a public start URL whose hostname resolves to IPv6 loopback', async () => {
    const rebindingLookup: DnsLookupFn = async () => [
      { address: '::1', family: 6 },
    ];
    let fetchCalled = false;
    const fetchImpl = async () => {
      fetchCalled = true;
      return fakeResponse('<html></html>');
    };
    const result = await crawlSite('https://attacker.example/', {
      fetchImpl,
      dnsLookup: rebindingLookup,
    });
    expect(result.pages).toEqual([]);
    expect(fetchCalled).toBe(false);
  });

  it('rejects when DNS lookup fails (fail-closed)', async () => {
    const failingLookup: DnsLookupFn = async () => {
      throw new Error('ENOTFOUND');
    };
    const result = await crawlSite('https://attacker.example/', {
      fetchImpl: makeFetch({}),
      dnsLookup: failingLookup,
    });
    expect(result.pages).toEqual([]);
    expect(
      result.warnings.some((w) => /DNS|ENOTFOUND/i.test(w))
    ).toBe(true);
  });

  it('rejects a redirect to a hostname that resolves to a private IP', async () => {
    // Lookup table keyed by hostname: start is public, redirect target
    // resolves to a private IP at fetch time (the rebinding scenario).
    // The target is the same site (www variant), so the same-site rule lets
    // the hop through and it is the per-hop DNS check that has to stop it.
    const lookup: DnsLookupFn = async (hostname) => {
      if (hostname === 'public.example') {
        return [{ address: '93.184.216.34', family: 4 }];
      }
      if (hostname === 'www.public.example') {
        return [{ address: '127.0.0.1', family: 4 }];
      }
      return [];
    };
    let leaked = 0;
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u === 'https://public.example/') {
        return {
          ok: false,
          status: 302,
          headers: new Headers({ location: 'https://www.public.example/secret' }),
          text: async () => '',
        } as unknown as Response;
      }
      // If we reach this branch, the redirect guard didn't fire.
      leaked += 1;
      return fakeResponse('<html><body><h1>leak</h1></body></html>');
    };
    const result = await crawlSite('https://public.example/', {
      fetchImpl,
      dnsLookup: lookup,
    });
    expect(result.pages).toEqual([]);
    expect(leaked).toBe(0);
    expect(
      result.warnings.some((w) =>
        /redirect DNS-rebinding guard rejected www\.public\.example.*127\.0\.0\.1/.test(w)
      )
    ).toBe(true);
  });

  it('allows crawl when DNS resolves to a public IP', async () => {
    const lookup: DnsLookupFn = async () => [
      { address: '93.184.216.34', family: 4 },
    ];
    const fetchImpl = makeFetch({
      'https://benign.example/': '<html><body><main><h1>Hi</h1><p>ok</p></main></body></html>',
    });
    const result = await crawlSite('https://benign.example/', {
      fetchImpl,
      dnsLookup: lookup,
    });
    expect(result.pages.length).toBe(1);
    expect(result.warnings).toEqual([]);
  });
});

describe('segmentHtmlToCopyPage XSS hardening', () => {
  it('strips inline tags from h1 (heading is plain text)', () => {
    const html = `
      <html><body><main>
        <h1>Title <script>alert(1)</script>End</h1>
        <p>body</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    // Strip path replaces stripped tags with a space, so we may see
    // double-spaces. The contract is "no tags survive," not exact
    // whitespace.
    expect(cp!.hero?.heading).toMatch(/^Title\s+End$/);
    expect(cp!.hero?.heading).not.toContain('<');
    expect(cp!.hero?.heading).not.toContain('script');
    expect(cp!.hero?.heading).not.toContain('alert');
  });

  it('escapes attribute-context characters in body_html', () => {
    const html = `
      <html><body><main>
        <h1>Site</h1>
        <p>Visit "the place" &lt;br&gt; we love.</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.hero?.body_html).toBeDefined();
    // Quotes + angle brackets must be entity-encoded inside the <p>.
    expect(cp!.hero?.body_html).not.toContain('"the place"');
    expect(cp!.hero?.body_html).toContain('&quot;');
    expect(cp!.hero?.body_html).toContain('&lt;');
    expect(cp!.hero?.body_html).toContain('&gt;');
  });

  it('does not produce a script tag in body_html when source contained one', () => {
    const html = `
      <html><body><main>
        <h1>Hi</h1>
        <p>Hello <script>alert(1)</script> there.</p>
      </main></body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.hero?.body_html).toBeDefined();
    expect(cp!.hero?.body_html).not.toContain('<script');
    expect(cp!.hero?.body_html).not.toContain('alert(1)');
  });
});

describe('segmentHtmlToCopyPage hero in <header>', () => {
  it('extracts H1 from inside <header> (Squarespace-style markup)', () => {
    const html = `
      <html><body>
        <header class="site-header">
          <h1>Studio Name</h1>
          <p>One short paragraph of intro.</p>
        </header>
        <main><p>More content.</p></main>
      </body></html>
    `;
    const cp = segmentHtmlToCopyPage('/', html);
    expect(cp!.hero?.heading).toBe('Studio Name');
    expect(cp!.hero?.body_html).toContain('One short paragraph');
  });
});

// =====================================================================
// Deep-media mining (v2-faithful-copy-deep-media): on a website-builder
// page the static HTML can expose a single image while script blobs hold
// the gallery photos and videos. The crawler mines same-origin /
// allowlisted-CDN script blobs for media candidates the operator can
// approve.
// =====================================================================

import {
  extractMediaUrls,
  extractScriptBlobUrls,
} from '../../src/services/site_copy_crawl';

describe('extractMediaUrls', () => {
  it('finds raster image URLs with and without query strings', () => {
    const text =
      'x "https://cdn.example.com/a/photo1.jpg" y https://img.wsimg.com/blobby/go/abc/p2.png?w=600 z';
    const out = extractMediaUrls(text);
    expect(out).toContain('https://cdn.example.com/a/photo1.jpg');
    expect(out).toContain('https://img.wsimg.com/blobby/go/abc/p2.png?w=600');
  });

  it('normalizes YouTube watch/embed/short forms to canonical URLs', () => {
    const text = [
      'https://www.youtube.com/watch?v=AbC123xYz_-',
      'https://www.youtube.com/embed/QwErTy123456',
      'https://youtu.be/ZxCvBn987654',
    ].join(' ');
    const out = extractMediaUrls(text);
    expect(out).toContain('https://www.youtube.com/watch?v=AbC123xYz_-');
    expect(out).toContain('https://www.youtube.com/watch?v=QwErTy123456');
    expect(out).toContain('https://www.youtube.com/watch?v=ZxCvBn987654');
  });

  it('normalizes Vimeo IDs', () => {
    const out = extractMediaUrls('see vimeo.com/video/123456789 now');
    expect(out).toContain('https://vimeo.com/123456789');
  });

  it('caps candidates at 40', () => {
    const many = Array.from(
      { length: 60 },
      (_, i) => `https://cdn.example.com/p${i}.jpg`
    ).join(' ');
    expect(extractMediaUrls(many).length).toBeLessThanOrEqual(40);
  });

  it('ignores non-media URLs', () => {
    expect(extractMediaUrls('https://example.com/page https://example.com/x.pdf')).toEqual([]);
  });
});

describe('extractScriptBlobUrls', () => {
  const origin = 'https://studio.example';

  it('keeps same-origin script srcs', () => {
    const html = '<script src="/widgets/script.js"></script>';
    expect(extractScriptBlobUrls(html, origin)).toEqual([
      'https://studio.example/widgets/script.js',
    ]);
  });

  it('keeps allowlisted site-builder CDN blobs (wsimg)', () => {
    const html =
      '<script src="https://img1.wsimg.com/blobby/go/site/gpub/hash/script.js"></script>';
    expect(extractScriptBlobUrls(html, origin)).toEqual([
      'https://img1.wsimg.com/blobby/go/site/gpub/hash/script.js',
    ]);
  });

  it('rejects arbitrary cross-origin scripts', () => {
    const html = '<script src="https://evil.example/x.js"></script>';
    expect(extractScriptBlobUrls(html, origin)).toEqual([]);
  });

  it('rejects non-http schemes', () => {
    const html = '<script src="data:text/javascript,alert(1)"></script>';
    expect(extractScriptBlobUrls(html, origin)).toEqual([]);
  });
});

describe('crawlSite deep-media mining', () => {
  it('mines media from pages and same-origin script blobs into media_candidates', async () => {
    const pageHtml = `<!DOCTYPE html><html><head>
      <script src="/gallery/script.js"></script>
      </head><body><main><h1>Home</h1>
      <p>Welcome.</p>
      <img src="https://studio.example/static/visible.jpg">
      </main></body></html>`;
    const blobJs =
      'var photos=["https://img1.wsimg.com/blobby/go/x/g1.jpg","https://img1.wsimg.com/blobby/go/x/g2.jpg"];' +
      'var video="https://www.youtube.com/watch?v=ReadingVid12";';
    const fetchImpl = (async (url: RequestInfo | URL) => {
      const u = String(url);
      const body = u.includes('script.js') ? blobJs : pageHtml;
      return new Response(body, {
        status: 200,
        headers: { 'content-type': u.includes('script.js') ? 'text/javascript' : 'text/html' },
      });
    }) as typeof fetch;
    const dnsLookup = async () => [{ address: '93.184.216.34', family: 4 }];
    const result = await crawlSite('https://studio.example/', {
      fetchImpl,
      dnsLookup,
      maxPages: 1,
    });
    expect(result.media_candidates).toContain('https://img1.wsimg.com/blobby/go/x/g1.jpg');
    expect(result.media_candidates).toContain('https://img1.wsimg.com/blobby/go/x/g2.jpg');
    expect(result.media_candidates).toContain('https://www.youtube.com/watch?v=ReadingVid12');
    expect(result.media_candidates).toContain('https://studio.example/static/visible.jpg');
  });

  it('returns empty media_candidates on guard-rejected URLs', async () => {
    const result = await crawlSite('not-a-url');
    expect(result.media_candidates).toEqual([]);
  });
});


// ============================================================
// Request policy: ports, credentials, redirects
// ============================================================

function redirectTo(location: string, status = 302): Response {
  return {
    ok: false,
    status,
    headers: new Headers({ location }),
    text: async () => '',
  } as unknown as Response;
}

describe('crawlTargetProblem (what may be requested at all)', () => {
  it('accepts plain http and https on their default ports', () => {
    expect(crawlTargetProblem(new URL('https://studio.example/'))).toBeNull();
    expect(crawlTargetProblem(new URL('http://studio.example/about'))).toBeNull();
    expect(crawlTargetProblem(new URL('https://studio.example:443/'))).toBeNull();
    expect(crawlTargetProblem(new URL('http://studio.example:80/'))).toBeNull();
  });

  it('refuses every port other than 80 and 443', () => {
    for (const port of [22, 25, 2375, 3000, 4000, 5432, 6379, 8080, 8443, 9200]) {
      expect(crawlTargetProblem(new URL(`http://studio.example:${port}/`))).toMatch(/port .* not allowed/);
    }
  });

  it('refuses credentials embedded in the URL', () => {
    expect(crawlTargetProblem(new URL('https://user:pw@studio.example/'))).toMatch(/credentials/);
  });

  it('refuses non-http schemes and textually private hosts', () => {
    expect(crawlTargetProblem(new URL('ftp://studio.example/'))).toMatch(/protocol/);
    expect(crawlTargetProblem(new URL('http://127.0.0.1/'))).toMatch(/private host/);
    expect(crawlTargetProblem(new URL('http://[::1]/'))).toMatch(/private host/);
  });
});

describe('isPrivateOrLoopbackHost: fully qualified (trailing dot) names', () => {
  it('treats "localhost." and friends like their undotted forms', () => {
    expect(isPrivateOrLoopbackHost('localhost.')).toBe(true);
    expect(isPrivateOrLoopbackHost('api.internal.')).toBe(true);
    expect(isPrivateOrLoopbackHost('printer.local.')).toBe(true);
    expect(isPrivateOrLoopbackHost('127.0.0.1.')).toBe(true);
    expect(isPrivateOrLoopbackHost('.')).toBe(true);
    expect(isPrivateOrLoopbackHost('studio.example.')).toBe(false);
  });
});

describe('isSameSiteHost', () => {
  it('treats a host and its www variant as one site', () => {
    expect(isSameSiteHost('studio.example', 'www.studio.example')).toBe(true);
    expect(isSameSiteHost('WWW.Studio.Example', 'studio.example')).toBe(true);
    expect(isSameSiteHost('studio.example', 'studio.example')).toBe(true);
  });

  it('does not treat other subdomains, suffixes or lookalikes as the same site', () => {
    expect(isSameSiteHost('studio.example', 'admin.studio.example')).toBe(false);
    expect(isSameSiteHost('studio.example', 'studio.example.attacker.test')).toBe(false);
    expect(isSameSiteHost('studio.example', 'wwwstudio.example')).toBe(false);
    expect(isSameSiteHost('studio.example', 'other.example')).toBe(false);
  });
});

describe('crawlSite request policy', () => {
  it('refuses a start URL on a non-web port without issuing any request', async () => {
    let fetched = 0;
    const result = await crawlSite('http://studio.example:6379/', {
      fetchImpl: async () => {
        fetched += 1;
        return fakeResponse('<h1>x</h1>');
      },
      dnsLookup: PUBLIC_DNS,
    });
    expect(fetched).toBe(0);
    expect(result.pages).toEqual([]);
    expect(result.warnings.join(' ')).toMatch(/port 6379 is not allowed/);
  });

  it('refuses a redirect to another port on the same host', async () => {
    const requested: string[] = [];
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = String(url);
      requested.push(u);
      if (u === 'https://studio.example/') return redirectTo('http://studio.example:8080/admin');
      return fakeResponse('<h1>internal admin</h1>');
    };
    const result = await crawlSite('https://studio.example/', { fetchImpl, dnsLookup: PUBLIC_DNS });
    expect(requested).toEqual(['https://studio.example/']);
    expect(result.pages).toEqual([]);
    expect(result.warnings.join(' ')).toMatch(/redirect refused: port 8080 is not allowed/);
  });

  it('does not follow a redirect to a different public site, and names it', async () => {
    const requested: string[] = [];
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = String(url);
      requested.push(u);
      if (u === 'https://studio.example/') return redirectTo('https://elsewhere.example/landing');
      return fakeResponse('<html><body><h1>Other site</h1><p>not ours</p></body></html>');
    };
    const result = await crawlSite('https://studio.example/', { fetchImpl, dnsLookup: PUBLIC_DNS });
    expect(requested).toEqual(['https://studio.example/']);
    expect(result.pages).toEqual([]);
    expect(result.warnings.join(' ')).toMatch(/different site was not followed: elsewhere\.example/);
  });

  it('follows example.com -> www.example.com and http -> https', async () => {
    const requested: string[] = [];
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = String(url);
      requested.push(u);
      if (u === 'http://studio.example/') return redirectTo('https://studio.example/', 301);
      if (u === 'https://studio.example/') return redirectTo('https://www.studio.example/', 301);
      if (u === 'https://www.studio.example/') {
        return fakeResponse('<html><body><main><h1>Studio</h1><p>Welcome in.</p></main></body></html>');
      }
      return redirectTo('https://nowhere.example/');
    };
    const result = await crawlSite('http://studio.example/', {
      fetchImpl,
      dnsLookup: PUBLIC_DNS,
      maxPages: 1,
    });
    expect(requested).toEqual([
      'http://studio.example/',
      'https://studio.example/',
      'https://www.studio.example/',
    ]);
    expect(result.pages.length).toBe(1);
    expect(result.warnings).toEqual([]);
  });

  it('validates the DNS answer again for every redirect hop', async () => {
    const lookups: string[] = [];
    const dnsLookup: DnsLookupFn = async (hostname) => {
      lookups.push(hostname);
      return [{ address: '93.184.216.34', family: 4 }];
    };
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = String(url);
      if (u === 'https://studio.example/') return redirectTo('https://www.studio.example/');
      return fakeResponse('<html><body><main><h1>Studio</h1><p>Hi.</p></main></body></html>');
    };
    await crawlSite('https://studio.example/', { fetchImpl, dnsLookup, maxPages: 1 });
    // Start check, first request, then the redirect hop under its own name.
    expect(lookups).toEqual(['studio.example', 'studio.example', 'www.studio.example']);
  });

  it('gives up after three redirect hops', async () => {
    let calls = 0;
    const fetchImpl = async (): Promise<Response> => {
      calls += 1;
      return redirectTo(`https://studio.example/loop${calls}`);
    };
    const result = await crawlSite('https://studio.example/', { fetchImpl, dnsLookup: PUBLIC_DNS });
    // The original request plus three followed hops.
    expect(calls).toBe(4);
    expect(result.pages).toEqual([]);
    expect(result.warnings.join(' ')).toMatch(/too many redirects/);
  });
});

// ============================================================
// Connection pinning (DNS rebinding) and transport limits
// ============================================================

type LookupResult = { err: NodeJS.ErrnoException | null; address: unknown; family: unknown };

function callLookup(
  lookup: ReturnType<typeof createGuardedLookup>,
  hostname: string,
  options: { all?: boolean; family?: number }
): Promise<LookupResult> {
  return new Promise((resolve) => {
    lookup(hostname, options as never, ((err: NodeJS.ErrnoException | null, address: unknown, family: unknown) =>
      resolve({ err, address, family })) as never);
  });
}

describe('createGuardedLookup (the socket-level DNS hook)', () => {
  it('hands the socket only addresses it has validated', async () => {
    const lookup = createGuardedLookup(async () => [
      { address: '93.184.216.34', family: 4 },
      { address: '2606:2800:220:1:248:1893:25c8:1946', family: 6 },
    ]);
    const all = await callLookup(lookup, 'studio.example', { all: true });
    expect(all.err).toBeNull();
    expect(all.address).toEqual([
      { address: '93.184.216.34', family: 4 },
      { address: '2606:2800:220:1:248:1893:25c8:1946', family: 6 },
    ]);
    const single = await callLookup(lookup, 'studio.example', {});
    expect(single).toEqual({ err: null, address: '93.184.216.34', family: 4 });
    const v6 = await callLookup(lookup, 'studio.example', { family: 6 });
    expect(v6.address).toBe('2606:2800:220:1:248:1893:25c8:1946');
  });

  it('fails the connection when the name resolves to a private address', async () => {
    const lookup = createGuardedLookup(async () => [{ address: '169.254.169.254', family: 4 }]);
    const result = await callLookup(lookup, 'rebind.example', { all: true });
    expect(result.err).not.toBeNull();
    expect(result.err!.message).toMatch(/resolves to private address 169\.254\.169\.254/);
    expect(result.address).toEqual([]);
  });

  it('fails when ANY address in a mixed answer is private', async () => {
    const lookup = createGuardedLookup(async () => [
      { address: '93.184.216.34', family: 4 },
      { address: '10.0.0.7', family: 4 },
    ]);
    const result = await callLookup(lookup, 'mixed.example', {});
    expect(result.err!.message).toMatch(/10\.0\.0\.7/);
  });

  it('fails closed on a resolver error, an empty answer and a malformed entry', async () => {
    const throwing = createGuardedLookup(async () => {
      throw new Error('ENOTFOUND');
    });
    expect((await callLookup(throwing, 'gone.example', {})).err!.message).toMatch(/ENOTFOUND/);
    const empty = createGuardedLookup(async () => []);
    expect((await callLookup(empty, 'empty.example', {})).err!.message).toMatch(/no DNS records/);
    const junk = createGuardedLookup(async () => [{ address: 'not-an-address', family: 4 }]);
    expect((await callLookup(junk, 'junk.example', {})).err!.message).toMatch(/malformed/);
  });
});

describe('createPinnedFetch (default transport) against a loopback server', () => {
  const servers: http.Server[] = [];

  function listen(handler: http.RequestListener): Promise<{ port: number; hits: () => number }> {
    let hits = 0;
    const server = http.createServer((req, res) => {
      hits += 1;
      handler(req, res);
    });
    servers.push(server);
    return new Promise((resolve) => {
      server.listen(0, '127.0.0.1', () => {
        resolve({ port: (server.address() as AddressInfo).port, hits: () => hits });
      });
    });
  }

  afterEach(async () => {
    await Promise.all(
      servers.splice(0).map(
        (server) =>
          new Promise<void>((resolve) => {
            server.closeAllConnections();
            server.close(() => resolve());
          })
      )
    );
  });

  const LOOPBACK_DNS: DnsLookupFn = async () => [{ address: '127.0.0.1', family: 4 }];
  const allowAll = (): boolean => false;

  it('never connects when the name resolves to a loopback address at connect time', async () => {
    const server = await listen((_req, res) => res.end('internal'));
    // The pre-flight check is not involved here at all: this is the lookup
    // the socket itself performs, which is where a rebinding answer lands.
    const pinned = createPinnedFetch({ dnsLookup: LOOPBACK_DNS });
    await expect(pinned(`http://rebind.example:${server.port}/`)).rejects.toThrow(
      /DNS-rebinding guard rejected rebind\.example: resolves to private address 127\.0\.0\.1/
    );
    expect(server.hits()).toBe(0);

    // Control: the same request with the address policy relaxed does reach
    // the server, so the refusal above was the guard and nothing else.
    const relaxed = createPinnedFetch({ dnsLookup: LOOPBACK_DNS, isBlockedAddress: allowAll });
    const res = await relaxed(`http://rebind.example:${server.port}/`);
    expect(await res.text()).toBe('internal');
    expect(server.hits()).toBe(1);
  });

  it('survives a rebinding resolver: public for the pre-flight checks, private for the connection', async () => {
    const server = await listen((_req, res) => {
      res.setHeader('content-type', 'text/html');
      res.end('<html><body><h1>cloud metadata</h1><p>secret</p></body></html>');
    });
    let answers = 0;
    const rebinding: DnsLookupFn = async () => {
      answers += 1;
      // crawlSite asks twice before connecting (start check, per-request
      // check); both see a public address. The connection's own lookup is
      // the third question and gets the private one.
      return answers <= 2
        ? [{ address: '93.184.216.34', family: 4 }]
        : [{ address: '127.0.0.1', family: 4 }];
    };
    const pinned = createPinnedFetch({ dnsLookup: rebinding });
    const result = await crawlSite('http://rebind.example/', {
      // Same transport crawlSite builds by default, pointed at the test
      // server's port (the crawler itself only allows 80 and 443).
      fetchImpl: ((url: string | URL, init?: RequestInit) =>
        pinned(String(url).replace('http://rebind.example/', `http://rebind.example:${server.port}/`), init)) as typeof fetch,
      dnsLookup: rebinding,
      maxPages: 1,
    });
    expect(answers).toBe(3);
    expect(server.hits()).toBe(0);
    expect(result.pages).toEqual([]);
    expect(result.warnings.join(' ')).toMatch(/resolves to private address 127\.0\.0\.1/);
  });

  it('with the real system resolver: a name that resolves to loopback is refused at connect time', async () => {
    const server = await listen((_req, res) => res.end('internal'));
    // No injected dnsLookup here: this goes through dns.lookup, the resolver
    // production uses. "localhost" is refused by name before any lookup, so
    // the policy below judges addresses only, which lets the name through to
    // the resolver and leaves the verdict to what it returns.
    const addressesOnly = (hostOrAddress: string): boolean =>
      /^[0-9.]+$|:/.test(hostOrAddress) && isPrivateOrLoopbackHost(hostOrAddress);
    const pinned = createPinnedFetch({ isBlockedAddress: addressesOnly });
    await expect(pinned(`http://localhost:${server.port}/`)).rejects.toThrow(
      /DNS-rebinding guard rejected localhost: resolves to private address (127\.0\.0\.1|::1)/
    );
    expect(server.hits()).toBe(0);
  });

  it('refuses a literal private address without consulting DNS', async () => {
    const server = await listen((_req, res) => res.end('internal'));
    let lookups = 0;
    const pinned = createPinnedFetch({
      dnsLookup: async () => {
        lookups += 1;
        return [{ address: '93.184.216.34', family: 4 }];
      },
    });
    await expect(pinned(`http://127.0.0.1:${server.port}/`)).rejects.toThrow(/private host blocked/);
    await expect(pinned(`http://[::ffff:127.0.0.1]:${server.port}/`)).rejects.toThrow(/private host blocked/);
    expect(lookups).toBe(0);
    expect(server.hits()).toBe(0);
  });

  it('re-checks the peer address of the connected socket', async () => {
    const server = await listen((_req, res) => res.end('internal'));
    // Policy that lets 127.0.0.1 through the lookup hook and blocks it from
    // then on: stands in for any path that reaches a socket without the hook.
    let sightings = 0;
    const blockOnSecondSighting = (hostOrAddress: string): boolean => {
      if (hostOrAddress !== '127.0.0.1') return false;
      sightings += 1;
      return sightings > 1;
    };
    const pinned = createPinnedFetch({ dnsLookup: LOOPBACK_DNS, isBlockedAddress: blockOnSecondSighting });
    await expect(pinned(`http://peer.example:${server.port}/`)).rejects.toThrow(
      /connected address is not public: 127\.0\.0\.1/
    );
    expect(server.hits()).toBe(0);
  });

  it('does not follow redirects itself', async () => {
    const server = await listen((req, res) => {
      if (req.url === '/') {
        res.writeHead(302, { location: '/next' });
        res.end();
        return;
      }
      res.end('followed');
    });
    const pinned = createPinnedFetch({ dnsLookup: LOOPBACK_DNS, isBlockedAddress: allowAll });
    const res = await pinned(`http://studio.example:${server.port}/`);
    expect(res.status).toBe(302);
    expect(res.headers.get('location')).toBe('/next');
    expect(server.hits()).toBe(1);
  });

  it('sends the request headers and the original Host, and decodes a gzip body', async () => {
    let seenHost = '';
    let seenAgent = '';
    const server = await listen((req, res) => {
      seenHost = String(req.headers.host);
      seenAgent = String(req.headers['user-agent']);
      res.writeHead(200, { 'content-type': 'text/html', 'content-encoding': 'gzip' });
      res.end(zlib.gzipSync('<h1>hello</h1>'));
    });
    const pinned = createPinnedFetch({ dnsLookup: LOOPBACK_DNS, isBlockedAddress: allowAll });
    const res = await pinned(`http://studio.example:${server.port}/`, {
      headers: { 'User-Agent': 'crawl-test/1.0' },
    });
    expect(await res.text()).toBe('<h1>hello</h1>');
    expect(res.headers.get('content-encoding')).toBeNull();
    expect(seenHost).toBe(`studio.example:${server.port}`);
    expect(seenAgent).toBe('crawl-test/1.0');
  });

  it('aborts on a response header block larger than 16 KiB', async () => {
    const server = await listen((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/html', 'x-padding': 'p'.repeat(20 * 1024) });
      res.end('<h1>x</h1>');
    });
    const pinned = createPinnedFetch({ dnsLookup: LOOPBACK_DNS, isBlockedAddress: allowAll });
    await expect(pinned(`http://studio.example:${server.port}/`)).rejects.toThrow(/header/i);
  });

  it('applies the body cap to the decompressed size (compressed bomb)', async () => {
    // 64 MiB of one repeated byte compresses to roughly 64 KiB.
    const bomb = zlib.gzipSync(Buffer.alloc(64 * 1024 * 1024, 0x61));
    expect(bomb.length).toBeLessThan(200 * 1024);
    const server = await listen((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/html', 'content-encoding': 'gzip' });
      res.end(bomb);
    });
    const pinned = createPinnedFetch({ dnsLookup: LOOPBACK_DNS, isBlockedAddress: allowAll });
    const before = process.memoryUsage().arrayBuffers;
    const result = await crawlSite('http://bomb.example/', {
      fetchImpl: ((url: string | URL, init?: RequestInit) =>
        pinned(String(url).replace('http://bomb.example/', `http://bomb.example:${server.port}/`), init)) as typeof fetch,
      dnsLookup: PUBLIC_DNS,
      maxBodyBytes: 64 * 1024,
      maxPages: 1,
    });
    const grown = process.memoryUsage().arrayBuffers - before;
    // The page is one long run of "a" with no tags, so nothing is extracted;
    // what matters is that the crawl returned without inflating 64 MiB.
    expect(result.pages).toEqual([]);
    expect(grown).toBeLessThan(16 * 1024 * 1024);
  });

  it('honours the abort signal (the per-request timeout)', async () => {
    const server = await listen(() => {
      /* never answers */
    });
    const pinned = createPinnedFetch({ dnsLookup: LOOPBACK_DNS, isBlockedAddress: allowAll });
    const controller = new AbortController();
    const pending = pinned(`http://slow.example:${server.port}/`, { signal: controller.signal });
    setTimeout(() => controller.abort(), 50);
    await expect(pending).rejects.toThrow(/abort/i);
  });
});

// ============================================================
// Lengths of what the crawl follows and reports
// ============================================================

describe('crawlSite bounds the length of what it follows and reports', () => {
  // The remote site decides how long a link, a script URL or an image URL
  // is. Page paths go on to the translator (harbor_to_compose.ts), which
  // keeps 200 characters of one; URLs go into warnings and the stored result.

  it('does not follow a linked path longer than 200 characters, and says so in one warning', async () => {
    const tooLong = '/' + 'a'.repeat(200); // 201 characters
    const longestAllowed = '/' + 'b'.repeat(199); // 200 characters
    const slashRun = '/c' + '/'.repeat(300) + 'd';
    const home =
      `<a href="${tooLong}">too long</a><a href="${longestAllowed}">fits</a>` +
      `<a href="${slashRun}">slashes</a><h1>Home</h1><p>Welcome in.</p>`;
    const requested: string[] = [];
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      requested.push(String(url));
      return fakeResponse(
        String(url) === 'https://studio.example/' ? home : '<h1>Sub page</h1><p>More.</p>'
      );
    };
    const result = await crawlSite('https://studio.example/', { fetchImpl, dnsLookup: PUBLIC_DNS });
    expect(requested).toEqual(['https://studio.example/', `https://studio.example${longestAllowed}`]);
    expect(result.pages.map((p) => p.path)).toEqual(['/', longestAllowed]);
    expect(result.warnings).toEqual(['2 linked path(s) longer than 200 characters were not crawled']);
  });

  it('refuses a start URL whose path is longer than 200 characters before any lookup or request', async () => {
    let lookups = 0;
    let fetched = 0;
    const result = await crawlSite('https://studio.example/' + 'x/'.repeat(150), {
      fetchImpl: async () => {
        fetched += 1;
        return fakeResponse('<h1>x</h1>');
      },
      dnsLookup: async () => {
        lookups += 1;
        return [{ address: '93.184.216.34', family: 4 }];
      },
    });
    expect(result.pages).toEqual([]);
    expect(result.warnings).toEqual(['source_url rejected: its path is longer than 200 characters']);
    expect(lookups).toBe(0);
    expect(fetched).toBe(0);
  });

  it('drops media URLs longer than 2048 characters and does not fetch a script URL that long', async () => {
    const longImage = 'https://cdn.example.com/' + 'p'.repeat(2100) + '.jpg';
    const longScript = '/' + 's'.repeat(2100) + '.js';
    const home =
      `<script src="${longScript}"></script><script src="/ok.js"></script>` +
      `<h1>Home</h1><p>Welcome in.</p><img src="${longImage}">` +
      '<img src="https://cdn.example.com/short.jpg">';
    const blob = `var a = "${longImage}"; var b = "https://cdn.example.com/from-blob.png";`;
    const requested: string[] = [];
    const fetchImpl = async (url: string | URL): Promise<Response> => {
      const u = String(url);
      requested.push(u);
      return u.endsWith('.js') ? fakeResponse(blob, 'text/javascript') : fakeResponse(home);
    };
    const result = await crawlSite('https://studio.example/', {
      fetchImpl,
      dnsLookup: PUBLIC_DNS,
      maxPages: 1,
    });
    expect(requested).toEqual(['https://studio.example/', 'https://studio.example/ok.js']);
    expect(result.media_candidates).toEqual([
      'https://cdn.example.com/short.jpg',
      'https://cdn.example.com/from-blob.png',
    ]);
    expect(result.warnings).toEqual([]);
  });
});
