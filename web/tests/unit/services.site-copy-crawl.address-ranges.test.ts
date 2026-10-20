// The crawler's address policy for the IPv6 transition, deprecated and
// special-use ranges and the IPv4 documentation and 6to4 relay ranges.
//
// A URL whose host is a literal address is never looked up, so for it the
// textual check (isPrivateOrLoopbackHost, through crawlTargetProblem and the
// pinned fetch) is the whole guard. Each range below either embeds an IPv4
// address that a translator or relay on the path may dial (6to4, NAT64,
// IPv4-translated, Teredo, the deprecated IPv4-compatible form), is routed
// only inside a site (site-local), is not a single host (multicast), or is
// reserved and never a real site (discard-only, documentation).

import { describe, it, expect, afterEach, vi } from 'vitest';
import http from 'http';
import net from 'net';
import type { AddressInfo } from 'net';
import {
  crawlTargetProblem,
  createPinnedFetch,
  isPrivateOrLoopbackHost,
  type DnsLookupFn,
} from '../../src/services/site_copy_crawl';

/** URL, and the address a resolver would answer for it, for each range. */
const REFUSED: Array<{ url: string; address: string; family: 4 | 6 }> = [
  { url: 'http://[fec0::1]/', address: 'fec0::1', family: 6 }, // site-local
  { url: 'http://[2002:7f00:1::1]/', address: '2002:7f00:1::1', family: 6 }, // 6to4 of 127.0.0.1
  { url: 'http://[64:ff9b:1::7f00:1]/', address: '64:ff9b:1::7f00:1', family: 6 }, // local-use NAT64
  { url: 'http://[::7f00:1]/', address: '::7f00:1', family: 6 }, // IPv4-compatible 127.0.0.1
  { url: 'http://[::ffff:0:7f00:1]/', address: '::ffff:0:7f00:1', family: 6 }, // IPv4-translated
  { url: 'http://[ff02::1]/', address: 'ff02::1', family: 6 }, // multicast
  { url: 'http://[100::1]/', address: '100::1', family: 6 }, // discard-only
  { url: 'http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/', address: '2001:0:4136:e378:8000:63bf:3fff:fdd2', family: 6 }, // Teredo
  { url: 'http://[2001:db8::1]/', address: '2001:db8::1', family: 6 }, // documentation
  { url: 'http://192.0.2.10/', address: '192.0.2.10', family: 4 }, // TEST-NET-1
  { url: 'http://198.51.100.7/', address: '198.51.100.7', family: 4 }, // TEST-NET-2
  { url: 'http://203.0.113.9/', address: '203.0.113.9', family: 4 }, // TEST-NET-3
  { url: 'http://192.88.99.1/', address: '192.88.99.1', family: 4 }, // 6to4 relay anycast
];

describe('crawlTargetProblem: transition, deprecated and documentation ranges', () => {
  it.each(REFUSED)('refuses $url', ({ url }) => {
    expect(crawlTargetProblem(new URL(url))).toEqual(expect.stringMatching(/private host blocked/));
  });

  it('still allows the public addresses next to them', () => {
    for (const url of [
      'http://[2606:4700:4700::1111]/',
      'http://[2001:4860:4860::8888]/', // 2001::/32 is Teredo only, not all of 2001::/16
      'http://[2003::1]/',
      'http://[fe00::1]/',
      'http://8.8.8.8/',
      'http://192.0.3.1/',
      'http://203.0.114.1/',
      'http://[::ffff:8.8.8.8]/',
    ]) {
      expect(crawlTargetProblem(new URL(url)), url).toBeNull();
    }
  });
});

describe('createPinnedFetch: transition, deprecated and documentation ranges', () => {
  const servers: http.Server[] = [];

  function listen(): Promise<{ port: number; connections: () => number }> {
    let connections = 0;
    const server = http.createServer((_req, res) => res.end('internal'));
    server.on('connection', () => {
      connections += 1;
    });
    servers.push(server);
    return new Promise((resolve) => {
      server.listen(0, '127.0.0.1', () => {
        resolve({ port: (server.address() as AddressInfo).port, connections: () => connections });
      });
    });
  }

  afterEach(async () => {
    vi.restoreAllMocks();
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

  it('refuses each literal address before it opens a socket', async () => {
    // A socket the guard lets through is refused here, before a packet is
    // sent: these addresses are not this machine's, and a test sends nothing
    // off it.
    const connect = vi.spyOn(net.Socket.prototype, 'connect').mockImplementation((() => {
      throw new Error('test: a socket was opened');
    }) as never);
    const pinned = createPinnedFetch();
    const outcomes = await Promise.allSettled(REFUSED.map(({ url }) => pinned(url)));
    outcomes.forEach((outcome, index) => {
      expect(outcome.status, REFUSED[index].url).toBe('rejected');
      expect(String((outcome as PromiseRejectedResult).reason), REFUSED[index].url).toMatch(/private host blocked/);
    });
    expect(connect).not.toHaveBeenCalled();
  });

  it('refuses a DNS answer that holds one of these addresses, and the loopback server sees no connection', async () => {
    const server = await listen();
    // The address policy is the crawler's own, except that the test server's
    // address is let through: a name whose answer is [127.0.0.1, <address>]
    // then reaches the server only if <address> passes the policy.
    const policy = (hostOrAddress: string): boolean =>
      hostOrAddress !== '127.0.0.1' && isPrivateOrLoopbackHost(hostOrAddress);
    for (const { address, family } of REFUSED) {
      const answer: DnsLookupFn = async () => [
        { address: '127.0.0.1', family: 4 },
        { address, family },
      ];
      const pinned = createPinnedFetch({ dnsLookup: answer, isBlockedAddress: policy });
      await expect(
        pinned(`http://transition.example:${server.port}/`, { signal: AbortSignal.timeout(3000) }),
        address
      ).rejects.toThrow(`resolves to private address ${address}`);
    }
    expect(server.connections()).toBe(0);

    // Control: an answer of the server's address alone does reach it, so the
    // refusals above were the policy and nothing else.
    const control = createPinnedFetch({
      dnsLookup: async () => [{ address: '127.0.0.1', family: 4 }],
      isBlockedAddress: policy,
    });
    expect(await (await control(`http://transition.example:${server.port}/`)).text()).toBe('internal');
    expect(server.connections()).toBe(1);
  });
});
