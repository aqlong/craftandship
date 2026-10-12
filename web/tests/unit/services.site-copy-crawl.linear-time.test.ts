import { describe, it, expect } from 'vitest';
import {
  crawlSite,
  segmentHtmlToCopyPage,
  extractInternalLinks,
  extractScriptBlobUrls,
  extractMediaUrls,
  normalizePath,
  type DnsLookupFn,
} from '../../src/services/site_copy_crawl';

/**
 * The faithful-copy crawler parses pages a remote site controls, up to its
 * 2 MiB body cap. These tests hold it to two things:
 *
 *  1. Every parsing entry point takes time proportional to its input on the
 *     hostile shapes below, all the way to a full-size body. A backtracking
 *     pattern that rescans the rest of the page from every unclosed tag takes
 *     four times as long for each doubling; at these sizes that is seconds at
 *     128 KB and minutes at 2 MiB, far over the allowance.
 *  2. While `crawlSite` parses the slowest page found, the event loop of the
 *     calling process keeps running timers.
 *
 * Only what the module exported before the scanners were written is used
 * here, so the file can be run unchanged against an older version of the
 * module to see it fail.
 */

const KB = 1024;
const FULL_BODY = 2 * KB * KB; // DEFAULT_MAX_BODY_BYTES in the crawler
const SIZES = [8 * KB, 32 * KB, 128 * KB, 512 * KB, FULL_BODY];
const ORIGIN = 'https://studio.example';

/**
 * Time allowed for one call on `bytes` of input: half a second, plus three
 * milliseconds per KB (about 6.6 s for a full body). The scanners need a few
 * hundred milliseconds for the slowest full-size input on a laptop, so this
 * leaves a loaded CI runner more than an order of magnitude of room and still
 * sits well under what quadratic growth costs from 128 KB upward.
 */
function allowedMs(bytes: number): number {
  return 500 + (bytes / KB) * 3;
}

function fill(unit: string, bytes: number): string {
  return unit.repeat(Math.max(1, Math.floor(bytes / unit.length)));
}

const ENTRY_POINTS: Array<[string, (input: string) => unknown]> = [
  ['segmentHtmlToCopyPage', (input) => segmentHtmlToCopyPage('/', input)],
  ['extractInternalLinks', (input) => extractInternalLinks(input, ORIGIN)],
  ['extractScriptBlobUrls', (input) => extractScriptBlobUrls(input, ORIGIN)],
  ['extractMediaUrls', (input) => extractMediaUrls(input)],
  ['normalizePath', (input) => normalizePath(input)],
];

type Family = Record<string, (bytes: number) => string>;

const UNTERMINATED_COMMENTS: Family = {
  'comment openers back to back': (n) => fill('<!--', n),
  'comment openers with text': (n) => fill('<!-- x ', n),
  'closed comments, densely packed': (n) => fill('<!-- x -->', n),
};

const UNTERMINATED_NON_CONTENT_TAGS: Family = {
  '<script with no >': (n) => fill('<script ', n),
  '<script> never closed': (n) => fill('<script>', n),
  '<style> never closed': (n) => fill('<style>', n),
  '<nav> never closed': (n) => fill('<nav>', n),
  'every stripped tag opened, none closed': (n) =>
    fill('<script><style><noscript><template><iframe><svg><head><nav><footer><aside>', n),
  '<script src=" with no closing quote': (n) => fill('<script src="x', n),
  '<script src= with no quote': (n) => fill('<script src=', n),
  '<script data-src=" repeated': (n) => fill('<script data-src="', n),
};

const UNTERMINATED_BLOCK_TAGS: Family = {
  '<title> never closed': (n) => fill('<title>', n),
  '<title with no >': (n) => fill('<title ', n),
  '<p> never closed': (n) => fill('<p>', n),
  '<p with no >': (n) => fill('<p ', n),
  '<p with no >, then one </p>': (n) => fill('<p ', n) + '</p>',
  '<h1> never closed': (n) => fill('<h1>', n),
  '<li> never closed': (n) => fill('<li>', n),
  'h1, then <p> never closed (hero body)': (n) => '<h1>T</h1>' + fill('<p>', n),
  'h1, then empty paragraphs (hero body)': (n) => '<h1>T</h1>' + fill('<p> </p>', n),
};

const UNTERMINATED_INLINE_TAGS: Family = {
  '<b> never closed, inside a paragraph': (n) => '<p>' + fill('<b>', n) + '</p>',
  '<i> never closed, inside a paragraph': (n) => '<p>' + fill('<i>', n) + '</p>',
  '<strong with no >, inside a paragraph': (n) => '<p>' + fill('<strong ', n) + '</p>',
  '<a href> never closed, inside a paragraph': (n) => '<p>' + fill('<a href="x">', n) + '</p>',
  '<a href> never closed': (n) => fill('<a href="/x">', n),
};

const LONG_ATTRIBUTE_RUNS: Family = {
  'one <a with many href attributes and no >': (n) => '<a ' + fill('href="x" ', n),
  'the same inside a paragraph': (n) => '<p><a ' + fill('href="x" ', n) + '</p>',
  'one <a with many href attributes, closed': (n) => '<a ' + fill('href="x" ', n) + '>t</a>',
  '<a with attributes but no href': (n) => fill('<a x ', n),
  '<a href=" with no closing quote': (n) => fill('<a href="x', n),
  '<a href="" (empty) repeated': (n) => fill('<a href="" ', n),
  '<a href followed by a long run of spaces': (n) => '<a href' + fill(' ', n) + '=',
  '<a href= followed by a run of quotes': (n) => '<a href=' + fill('"', n),
  'one <script with many src attributes and no >': (n) => '<script ' + fill('src="x" ', n),
  'one <script with many src attributes, closed': (n) =>
    '<script ' + fill('src="/a.js" ', n) + '></script>',
};

const NESTED_TAGS: Family = {
  'deeply nested divs': (n) => fill('<div>', n / 2) + fill('</div>', n / 2),
  '<p><b><i> opened over and over': (n) => fill('<p><b><i>', n),
  'closed inline pairs inside one paragraph': (n) => '<p>' + fill('<b><i>x</i></b>', n) + '</p>',
  'mixed-case open and close tags': (n) => fill('<P>x</p><B>y</B>', n),
};

const DELIMITER_AND_WHITESPACE_RUNS: Family = {
  'a run of <': (n) => fill('<', n),
  'a run of < inside a paragraph': (n) => '<p>' + fill('<', n) + '</p>',
  'a run of >': (n) => fill('>', n),
  'a run of double quotes': (n) => fill('"', n),
  'a run of </': (n) => fill('</', n),
  'a run of </p>': (n) => fill('</p>', n),
  'a tag name that never ends': (n) => '<' + fill('a', n),
  'spaces inside a paragraph': (n) => '<p>' + fill(' ', n) + 'x</p>',
  'newlines inside a paragraph': (n) => '<p>a' + fill('\n', n) + 'b</p>',
  '<br followed by a long run of spaces': (n) => '<p><br' + fill(' ', n) + 'x</p>',
  '<br followed by spaces, many times': (n) => '<p>' + fill('<br' + ' '.repeat(512), n) + '</p>',
  'slashes in an href path': (n) => '<a href="/x' + fill('/', n) + 'a">x</a>',
  'slashes in many href paths': (n) => fill('<a href="/x' + '/'.repeat(2048) + 'a">x</a>', n),
  'a bare run of slashes (as a path)': (n) => fill('/', n) + 'a',
  'a bare run of question marks, then a line break (as a path)': (n) => fill('?', n) + '\n',
};

const UNTERMINATED_ENTITIES: Family = {
  'one hex reference that never ends': (n) => '<p>&#x' + fill('A', n) + '</p>',
  'hex reference openers back to back': (n) => '<p>' + fill('&#x', n) + '</p>',
  'one decimal reference that never ends': (n) => '<p>&#' + fill('9', n) + '</p>',
  'named references with no semicolon': (n) => '<p>' + fill('&amp', n) + '</p>',
};

const MEDIA_URL_RUNS: Family = {
  'http:// over and over with no extension': (n) => fill('http://a', n),
  'http:// with dots but no image extension': (n) => fill('http://a.', n),
  'http:// over and over, one extension at the very end': (n) => fill('http://a', n) + '.png',
  'a query string made of http://': (n) => 'https://a/b.jpg?' + fill('http://', n),
  'a long run of dots after http://': (n) => 'http://a' + fill('.', n),
  'image URLs back to back': (n) => fill('http://a.jpg', n),
  'youtube prefixes with no id': (n) => fill('youtube.com/watch?v=', n),
  'vimeo prefixes with short ids': (n) => fill('vimeo.com/video/12345', n),
  'sms: followed by a long run of digits': (n) => '<a href="sms:' + fill('1', n),
  'the word href followed by a space, repeated': (n) => fill('href ', n),
};

const ORDINARY_PAGES: Family = {
  'many ordinary paragraphs': (n) =>
    '<html><body><main>' +
    fill('<p>Plain <b>bold</b> and <a href="/about">a link</a>.</p>\n', n) +
    '</main></body></html>',
  'many links to the same page': (n) => fill('<a href="/page-about">About</a>\n', n),
};

const FAMILIES: Array<[string, Family]> = [
  ['unterminated comments', UNTERMINATED_COMMENTS],
  ['unterminated script, style and other stripped tags', UNTERMINATED_NON_CONTENT_TAGS],
  ['unterminated title and block tags', UNTERMINATED_BLOCK_TAGS],
  ['unterminated inline tags', UNTERMINATED_INLINE_TAGS],
  ['long attribute runs', LONG_ATTRIBUTE_RUNS],
  ['nested tags', NESTED_TAGS],
  ['long runs of one delimiter or of whitespace', DELIMITER_AND_WHITESPACE_RUNS],
  ['unterminated entities', UNTERMINATED_ENTITIES],
  ['media URL runs', MEDIA_URL_RUNS],
  ['control: ordinary markup at full size', ORDINARY_PAGES],
];

/**
 * Runs every entry point on growing inputs of one shape. Each entry point
 * stops at the first size that overruns its allowance, so a quadratic
 * implementation fails here in seconds instead of running the larger sizes.
 */
function overruns(build: (bytes: number) => string): string[] {
  const found: string[] = [];
  for (const [name, call] of ENTRY_POINTS) {
    for (const bytes of SIZES) {
      const input = build(bytes);
      const started = performance.now();
      call(input);
      const elapsed = performance.now() - started;
      if (elapsed > allowedMs(bytes)) {
        found.push(
          `${name}: ${Math.round(elapsed)} ms on ${Math.round(input.length / KB)} KB ` +
            `(allowed ${Math.round(allowedMs(bytes))} ms)`
        );
        break;
      }
    }
  }
  return found;
}

describe('faithful-copy parsing takes time proportional to its input', () => {
  for (const [familyName, family] of FAMILIES) {
    describe(familyName, () => {
      it.each(Object.keys(family))('%s', (shape) => {
        expect(overruns(family[shape])).toEqual([]);
      });
    });
  }

  it('the largest size tried is the crawler body cap, and the shapes reach it', () => {
    expect(SIZES[SIZES.length - 1]).toBe(2 * 1024 * 1024);
    for (const [, family] of FAMILIES) {
      for (const build of Object.values(family)) {
        // Within one repeat unit (the longest is about 2 KB) of the full size.
        expect(build(FULL_BODY).length).toBeGreaterThan(FULL_BODY - 4 * KB);
      }
    }
  });
});

// ============================================================
// The event loop during a worst-case parse
// ============================================================

const PUBLIC_DNS: DnsLookupFn = async () => [{ address: '93.184.216.34', family: 4 }];

function htmlResponse(body: string): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'text/html' }),
    text: async () => body,
  } as unknown as Response;
}

/**
 * Runs `work` while a 5 ms timer ticks, and reports the longest stretch in
 * which the timer could not fire.
 */
async function longestTimerSilence<T>(
  work: () => Promise<T>
): Promise<{ value: T; longestMs: number; ticks: number }> {
  let last = performance.now();
  let longestMs = 0;
  let ticks = 0;
  const timer = setInterval(() => {
    const now = performance.now();
    longestMs = Math.max(longestMs, now - last);
    last = now;
    ticks += 1;
  }, 5);
  try {
    const value = await work();
    longestMs = Math.max(longestMs, performance.now() - last);
    return { value, longestMs, ticks };
  } finally {
    clearInterval(timer);
  }
}

describe('crawlSite keeps the event loop free while it parses', () => {
  // The slowest full-size shape found for the scanners (a run of "<", which
  // every pass has to step over one character at a time), followed by 200 KB
  // of unterminated comments. A little real content comes first so the
  // result shows the page was parsed to the end.
  const HOSTILE_TAIL = fill('<!--', 200 * KB);
  const worstCasePage =
    '<title>Big</title><h1>Big</h1><p>Still parsed.</p>' +
    fill('<', FULL_BODY - HOSTILE_TAIL.length - 64) +
    HOSTILE_TAIL;

  it('a timer keeps firing during a full-size worst-case parse', async () => {
    expect(worstCasePage.length).toBeGreaterThan(FULL_BODY - KB);
    expect(worstCasePage.length).toBeLessThanOrEqual(FULL_BODY);

    // What the same work costs when it runs on the calling thread: this is
    // how long the loop would be held if the crawl parsed in place.
    const startedInPlace = performance.now();
    const inPlace = segmentHtmlToCopyPage('/', worstCasePage);
    extractInternalLinks(worstCasePage, ORIGIN);
    extractScriptBlobUrls(worstCasePage, ORIGIN);
    extractMediaUrls(worstCasePage);
    const inPlaceMs = performance.now() - startedInPlace;

    const fetchImpl = (async () => htmlResponse(worstCasePage)) as unknown as typeof fetch;
    const { value: result, longestMs, ticks } = await longestTimerSilence(() =>
      crawlSite('https://big.example/', { fetchImpl, dnsLookup: PUBLIC_DNS, maxPages: 1 })
    );

    // The page was parsed, whole, and the answer is the one parsing in place gives.
    expect(result.warnings).toEqual([]);
    expect(result.pages).toEqual([inPlace]);
    expect(result.pages[0].title).toBe('Big');
    expect(result.pages[0].markdown_blocks).toEqual(['# Big', 'Still parsed.']);

    // The loop was never held for anything like the length of the parse.
    // The floor of 100 ms absorbs scheduling noise on a busy machine.
    expect(ticks).toBeGreaterThan(0);
    expect(longestMs).toBeLessThan(Math.max(100, inPlaceMs * 0.4));
  });
});
