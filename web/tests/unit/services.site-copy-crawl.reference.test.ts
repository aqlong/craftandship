import { describe, it, expect } from 'vitest';
import {
  segmentHtmlToCopyPage,
  extractInternalLinks,
  extractScriptBlobUrls,
  extractMediaUrls,
  normalizePath,
} from '../../src/services/site_copy_crawl';

/**
 * The crawler's parsing functions are hand-written scanners that reproduce a
 * set of regular expressions exactly (each scanner in
 * src/services/site_copy_parse.ts names the expression it stands for). The
 * expressions themselves are not used by the application: on hostile input
 * they take time proportional to the square of the page, or worse.
 *
 * This file keeps those expressions as the REFERENCE for what the scanners
 * must return, and checks the two against each other: on a list of cases
 * chosen for the rules that are easy to get wrong, and on a few tens of
 * thousands of generated strings. The reference only ever sees short inputs
 * here, where its growth does not matter. Do not copy it into application
 * code.
 */

// ============================================================
// Reference implementation (regular expressions)
// ============================================================

const EM_DASH = String.fromCharCode(0x2014);
const REF_MAX_BLOCKS_PER_PAGE = 60;
const REF_MAX_BLOCK_LENGTH = 8000;
const REF_MAX_MEDIA_CANDIDATES = 40;
const REF_SCRIPT_BLOB_HOST_ALLOWLIST =
  /(^|\.)wsimg\.com$|(^|\.)squarespace\.com$|(^|\.)wixstatic\.com$/i;

interface RefCopyPage {
  path: string;
  title?: string;
  markdown_blocks: string[];
  hero?: { heading?: string; body_html?: string; cta_text_me_phone?: string };
}

function refExtractScriptBlobUrls(html: string, origin: string): string[] {
  const out: string[] = [];
  const re = /<script[^>]+src=["']([^"']+)["']/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) !== null) {
    let abs: URL;
    try {
      abs = new URL(m[1], origin);
    } catch {
      continue;
    }
    if (abs.protocol !== 'https:' && abs.protocol !== 'http:') continue;
    const sameOrigin = abs.origin === origin;
    const allowlisted = REF_SCRIPT_BLOB_HOST_ALLOWLIST.test(abs.hostname);
    if (!sameOrigin && !allowlisted) continue;
    if (!/\.js(\?|$)/i.test(abs.pathname + abs.search) && !allowlisted) continue;
    out.push(abs.toString());
  }
  return out;
}

function refExtractMediaUrls(text: string): string[] {
  const out = new Set<string>();
  const img = /https?:\/\/[^\s"'\\()<>]+?\.(?:jpe?g|png|webp|gif)(?:\?[^\s"'\\()<>]*)?/gi;
  let m: RegExpExecArray | null;
  while ((m = img.exec(text)) !== null) {
    out.add(m[0]);
    if (out.size >= REF_MAX_MEDIA_CANDIDATES) return [...out];
  }
  const yt = /(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)([A-Za-z0-9_-]{6,20})/g;
  while ((m = yt.exec(text)) !== null) {
    out.add(`https://www.youtube.com/watch?v=${m[1]}`);
    if (out.size >= REF_MAX_MEDIA_CANDIDATES) return [...out];
  }
  const vimeo = /vimeo\.com\/(?:video\/)?(\d{6,12})/g;
  while ((m = vimeo.exec(text)) !== null) {
    out.add(`https://vimeo.com/${m[1]}`);
    if (out.size >= REF_MAX_MEDIA_CANDIDATES) return [...out];
  }
  return [...out];
}

const REF_NON_CONTENT_TAGS = [
  'script',
  'style',
  'noscript',
  'template',
  'iframe',
  'svg',
  'head',
  'nav',
  'footer',
  'aside',
];

function refStripNonContent(html: string): string {
  let out = html;
  for (const tag of REF_NON_CONTENT_TAGS) {
    out = out.replace(new RegExp(`<${tag}\\b[^>]*>[\\s\\S]*?</${tag}>`, 'gi'), ' ');
  }
  return out.replace(/<!--[\s\S]*?-->/g, ' ');
}

function refStripTags(html: string): string {
  return html.replace(/<[^>]+>/g, '');
}

const REF_ENTITY_MAP: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
  mdash: EM_DASH,
  ndash: String.fromCharCode(0x2013),
  hellip: String.fromCharCode(0x2026),
  lsquo: String.fromCharCode(0x2018),
  rsquo: String.fromCharCode(0x2019),
  ldquo: String.fromCharCode(0x201c),
  rdquo: String.fromCharCode(0x201d),
};

function refCodePoint(codePoint: number, original: string): string {
  if (!Number.isInteger(codePoint) || codePoint < 0 || codePoint > 0x10ffff) return original;
  return String.fromCodePoint(codePoint);
}

function refDecodeHtml(s: string): string {
  return s
    .replace(/&#x([0-9a-fA-F]+);/g, (m, hex) => refCodePoint(parseInt(hex, 16), m))
    .replace(/&#(\d+);/g, (m, dec) => refCodePoint(parseInt(dec, 10), m))
    .replace(/&([a-zA-Z]+);/g, (m, name) => REF_ENTITY_MAP[name] ?? m);
}

function refSanitizeHref(href: string): string | null {
  const v = href.trim();
  if (!v) return null;
  if (/^javascript:/i.test(v)) return null;
  if (/^data:/i.test(v)) return null;
  if (/^(https?:|sms:|tel:|mailto:|\/|\.\.?\/|#)/i.test(v)) return v;
  if (/^[a-z0-9_./?=&%-]+$/i.test(v)) return v;
  return null;
}

function refRenderInline(html: string): string {
  let s = html;
  s = s.replace(/<br\s*\/?\s*>/gi, '\n');
  s = s.replace(
    /<(b|strong)\b[^>]*>([\s\S]*?)<\/\1>/gi,
    (_m, _t, inner) => `**${refStripTags(inner).trim()}**`
  );
  s = s.replace(
    /<(i|em)\b[^>]*>([\s\S]*?)<\/\1>/gi,
    (_m, _t, inner) => `*${refStripTags(inner).trim()}*`
  );
  s = s.replace(
    /<a\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi,
    (_m, href, inner) => {
      const text = refStripTags(inner).trim();
      const safe = refSanitizeHref(String(href));
      if (!safe) return text;
      if (!text) return safe;
      return `[${text}](${safe})`;
    }
  );
  s = refStripTags(s);
  s = refDecodeHtml(s);
  s = s.replace(/[ \t\r\f]+/g, ' ').replace(/\n[ \t]+/g, '\n');
  s = s.replace(/\n{3,}/g, '\n\n');
  return s;
}

function refHtmlToMarkdownBlock(tag: string, innerHtml: string): string | null {
  const trimmed = refRenderInline(innerHtml).trim();
  if (!trimmed) return null;
  if (/^h([1-6])$/.test(tag)) return `${'#'.repeat(Number(tag.slice(1)))} ${trimmed}`;
  if (tag === 'li') return `- ${trimmed}`;
  if (tag === 'blockquote') return `> ${trimmed}`;
  return trimmed;
}

function refExtractBlocks(html: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const re = /<(h[1-6]|p|li|blockquote|figcaption|dt|dd)\b[^>]*>([\s\S]*?)<\/\1>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) !== null) {
    const md = refHtmlToMarkdownBlock(m[1].toLowerCase(), m[2]);
    if (!md) continue;
    if (md.length > REF_MAX_BLOCK_LENGTH) continue;
    if (seen.has(md)) continue;
    seen.add(md);
    out.push(md);
    if (out.length >= REF_MAX_BLOCKS_PER_PAGE) break;
  }
  return out;
}

function refEscapeHtmlText(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function refExtractHero(contentHtml: string, rawHtml: string): RefCopyPage['hero'] | undefined {
  const hero: NonNullable<RefCopyPage['hero']> = {};
  const h1 = contentHtml.match(/<h1\b[^>]*>([\s\S]*?)<\/h1>/i);
  if (h1) {
    const heading = refDecodeHtml(refStripTags(h1[1])).trim();
    if (heading) hero.heading = heading.slice(0, 400);
    const after = contentHtml.slice((h1.index ?? 0) + h1[0].length);
    const paraRe = /<p\b[^>]*>([\s\S]*?)<\/p>/gi;
    const parts: string[] = [];
    let pm: RegExpExecArray | null;
    let count = 0;
    while ((pm = paraRe.exec(after)) !== null && count < 2) {
      const md = refRenderInline(pm[1]).trim();
      if (md) {
        parts.push(`<p>${refEscapeHtmlText(md)}</p>`);
        count += 1;
      }
    }
    if (parts.length > 0) hero.body_html = parts.join('\n').slice(0, REF_MAX_BLOCK_LENGTH);
  }
  const sms = rawHtml.match(/href\s*=\s*["']sms:([+\d\s\-()]+)["']/i);
  if (sms) {
    const digits = sms[1].replace(/\D+/g, '');
    if (digits.length >= 7 && digits.length <= 15) hero.cta_text_me_phone = digits;
  }
  return Object.keys(hero).length > 0 ? hero : undefined;
}

function refSegmentHtmlToCopyPage(path: string, html: string): RefCopyPage | null {
  const stripped = refStripNonContent(html);
  const titleMatch = html.match(/<title\b[^>]*>([\s\S]*?)<\/title>/i);
  const title = titleMatch ? refDecodeHtml(refStripTags(titleMatch[1])).trim() || undefined : undefined;
  const blocks = refExtractBlocks(stripped);
  if (blocks.length === 0 && !title) return null;
  const hero = refExtractHero(stripped, html);
  const entry: RefCopyPage = { path, markdown_blocks: blocks.slice(0, REF_MAX_BLOCKS_PER_PAGE) };
  if (title) entry.title = title.slice(0, 200);
  if (hero) entry.hero = hero;
  return entry;
}

function refNormalizePath(p: string): string {
  let out = (p || '/').toLowerCase();
  out = out.replace(/[?#].*$/, '');
  if (!out.startsWith('/')) out = `/${out}`;
  if (out.length > 1) out = out.replace(/\/+$/, '');
  return out;
}

const REF_SKIP_PATH_FRAGMENTS = [
  '/wp-admin',
  '/wp-login',
  '/cart',
  '/checkout',
  '/account',
  '/login',
  '/signup',
];

function refExtractInternalLinks(html: string, origin: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const re = /<a\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) !== null) {
    const rawHref = m[1].trim();
    if (!rawHref) continue;
    if (rawHref.startsWith('#')) continue;
    if (/^(mailto:|tel:|sms:|javascript:|data:)/i.test(rawHref)) continue;
    let abs: URL;
    try {
      abs = new URL(rawHref, origin + '/');
    } catch {
      continue;
    }
    if (abs.origin !== origin) continue;
    const path = refNormalizePath(abs.pathname);
    if (path === '/') continue;
    if (!/^\/[a-z0-9/_-]*$/.test(path)) continue;
    if (REF_SKIP_PATH_FRAGMENTS.some((frag) => path.includes(frag))) continue;
    if (/\.(jpe?g|png|gif|webp|svg|pdf|zip|css|js|ico|xml|json)$/i.test(path)) continue;
    if (seen.has(path)) continue;
    seen.add(path);
    out.push(path);
  }
  out.sort((a, b) => a.localeCompare(b));
  return out;
}

// ============================================================
// Comparison
// ============================================================

const ORIGIN = 'https://studio.example';

const COMPARED: Array<[string, (input: string) => unknown, (input: string) => unknown]> = [
  [
    'segmentHtmlToCopyPage',
    (input) => segmentHtmlToCopyPage('/', input),
    (input) => refSegmentHtmlToCopyPage('/', input),
  ],
  [
    'extractInternalLinks',
    (input) => extractInternalLinks(input, ORIGIN),
    (input) => refExtractInternalLinks(input, ORIGIN),
  ],
  [
    'extractScriptBlobUrls',
    (input) => extractScriptBlobUrls(input, ORIGIN),
    (input) => refExtractScriptBlobUrls(input, ORIGIN),
  ],
  ['extractMediaUrls', (input) => extractMediaUrls(input), (input) => refExtractMediaUrls(input)],
  ['normalizePath', (input) => normalizePath(input), (input) => refNormalizePath(input)],
];

function blocksOf(html: string): string[] | null {
  const page = segmentHtmlToCopyPage('/', html);
  return page ? page.markdown_blocks : null;
}

describe('scanner rules that are easy to get wrong (each also checked against the reference)', () => {
  const cases: Array<[string, () => unknown, () => unknown, unknown]> = [
    [
      'of several hrefs in one tag, the last one is the link',
      () => extractInternalLinks('<a href="/first" href="/second">x</a>', ORIGIN),
      () => refExtractInternalLinks('<a href="/first" href="/second">x</a>', ORIGIN),
      ['/second'],
    ],
    [
      'an empty href is skipped in favour of an earlier usable one',
      () => extractInternalLinks('<a href="/kept" href="">x</a>', ORIGIN),
      () => refExtractInternalLinks('<a href="/kept" href="">x</a>', ORIGIN),
      ['/kept'],
    ],
    [
      'href needs a word boundary before it: data-href counts, xhref does not',
      () => extractInternalLinks('<a data-href="/d">x</a><a xhref="/no">y</a>', ORIGIN),
      () => refExtractInternalLinks('<a data-href="/d">x</a><a xhref="/no">y</a>', ORIGIN),
      ['/d'],
    ],
    [
      'the quotes around an href need not be the same kind',
      () => extractInternalLinks('<a href="/q\' class="x">y</a>', ORIGIN),
      () => refExtractInternalLinks('<a href="/q\' class="x">y</a>', ORIGIN),
      ['/q'],
    ],
    [
      'an href after the first > of its tag is not seen, even when that > is inside a quoted attribute',
      () => extractInternalLinks('<a title="a>b" href="/after">x</a><a href="/next">n</a>', ORIGIN),
      () => refExtractInternalLinks('<a title="a>b" href="/after">x</a><a href="/next">n</a>', ORIGIN),
      ['/next'],
    ],
    [
      'an anchor runs to the first </a>, so an unclosed one takes the next anchor as its text',
      () => extractInternalLinks('<a class="x"><a href="/open"><a href="/done">d</a>', ORIGIN),
      () => refExtractInternalLinks('<a class="x"><a href="/open"><a href="/done">d</a>', ORIGIN),
      ['/open'],
    ],
    [
      'a block runs to the first closing tag of its name; nesting is not tracked',
      () => blocksOf('<p>one<p>two</p>'),
      () => refSegmentHtmlToCopyPage('/', '<p>one<p>two</p>')!.markdown_blocks,
      ['onetwo'],
    ],
    [
      'a tag that is never closed yields nothing, and does not stop other tags',
      () => blocksOf('<li>a<li>b</ul><h2>Kept</h2>'),
      () => refSegmentHtmlToCopyPage('/', '<li>a<li>b</ul><h2>Kept</h2>')!.markdown_blocks,
      ['## Kept'],
    ],
    [
      'tag names match in any case, and <pre> is not <p>',
      () => blocksOf('<P>Upper</p><pre>not a block</pre><H2 id=x>Two</h2>'),
      () => refSegmentHtmlToCopyPage('/', '<P>Upper</p><pre>not a block</pre><H2 id=x>Two</h2>')!.markdown_blocks,
      ['Upper', '## Two'],
    ],
    [
      '<br>, <br/> and <br / > are line breaks; <br clear="all"> is just a tag',
      () => blocksOf('<p>a<br>b<br/>c<BR / >d<br clear="all">e</p>'),
      () => refSegmentHtmlToCopyPage('/', '<p>a<br>b<br/>c<BR / >d<br clear="all">e</p>')!.markdown_blocks,
      ['a\nb\nc\nde'],
    ],
    [
      '"<>" stays as text; "<", anything, ">" is removed as a tag',
      () => blocksOf('<p>x <> y</p><p>a < b and c > d</p>'),
      () => refSegmentHtmlToCopyPage('/', '<p>x <> y</p><p>a < b and c > d</p>')!.markdown_blocks,
      ['x <> y', 'a d'],
    ],
    [
      'stripped tags are removed one name at a time, in order (script before nav)',
      () => blocksOf('<nav><script></nav></script>X</nav><p>kept</p>'),
      () => refSegmentHtmlToCopyPage('/', '<nav><script></nav></script>X</nav><p>kept</p>')!.markdown_blocks,
      ['kept'],
    ],
    [
      'a comment ends at the first --> that starts after its opener',
      () => blocksOf('<!--><p>hidden</p>--><p>shown</p>'),
      () => refSegmentHtmlToCopyPage('/', '<!--><p>hidden</p>--><p>shown</p>')!.markdown_blocks,
      ['shown'],
    ],
    [
      'script src: data-src counts, the tag name is not delimited, the last src wins',
      () =>
        extractScriptBlobUrls(
          '<script data-src="/lazy.js"></script><scripts src="/s.js"><script src="/a.js" src="/b.js">',
          ORIGIN
        ),
      () =>
        refExtractScriptBlobUrls(
          '<script data-src="/lazy.js"></script><scripts src="/s.js"><script src="/a.js" src="/b.js">',
          ORIGIN
        ),
      ['https://studio.example/lazy.js', 'https://studio.example/s.js', 'https://studio.example/b.js'],
    ],
    [
      'script src: an empty value is skipped in favour of an earlier one, and src= needs something before it',
      () => extractScriptBlobUrls('<script src="/kept.js" src=""><scriptsrc="/no.js">', ORIGIN),
      () => refExtractScriptBlobUrls('<script src="/kept.js" src=""><scriptsrc="/no.js">', ORIGIN),
      ['https://studio.example/kept.js'],
    ],
    [
      'an image URL ends at its first extension; a query string directly after it is kept',
      () =>
        extractMediaUrls(
          'https://a.example/x.jpgs http://a.example/p.png?w=1)z https://a.example/http://b.example/c.gif'
        ),
      () =>
        refExtractMediaUrls(
          'https://a.example/x.jpgs http://a.example/p.png?w=1)z https://a.example/http://b.example/c.gif'
        ),
      [
        'https://a.example/x.jpg',
        'http://a.example/p.png?w=1',
        'https://a.example/http://b.example/c.gif',
      ],
    ],
    [
      'an image URL needs at least one character before the extension dot',
      () => extractMediaUrls('http://.png http://a.jpeg http://b.jpe'),
      () => refExtractMediaUrls('http://.png http://a.jpeg http://b.jpe'),
      ['http://a.jpeg'],
    ],
    [
      'normalizePath: trailing slashes go, even all of them',
      () => [normalizePath('/A/B//'), normalizePath('////')],
      () => [refNormalizePath('/A/B//'), refNormalizePath('////')],
      ['/a/b', ''],
    ],
    [
      'normalizePath: the query is cut at the first ? or # with no line break after it',
      () => [normalizePath('/a?b#c'), normalizePath('?x\n/y?z')],
      () => [refNormalizePath('/a?b#c'), refNormalizePath('?x\n/y?z')],
      ['/a', '/?x\n/y'],
    ],
  ];

  it.each(cases)('%s', (_rule, actual, reference, expected) => {
    expect(reference()).toEqual(expected);
    expect(actual()).toEqual(expected);
  });
});

// ============================================================
// Generated inputs
// ============================================================

/** Small deterministic generator, so a failure can be reproduced from its seed. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const fromCode = String.fromCharCode;

// Fragments that put the scanners' decision points next to each other in
// every order: openers without closers, attributes without quotes, the
// characters the patterns treat specially, and a few non-ASCII look-alikes
// (a no-break space, a line separator, a long s, a Kelvin sign, a dotted
// capital I) that must not be mistaken for ASCII.
const TOKENS = [
  '<', '>', '</', '/>', '<>', '<<', ' ', ' ', '\n', '\t', '\r', '\f', '\v',
  fromCode(0xa0), fromCode(0x2028), fromCode(0xfeff),
  '<p', '<p>', '</p>', '<P>', '</P>', '<p class="x">', '<p-x>', '<pre>', '</pre>', '<p1>',
  '<h1>', '</h1>', '<H1 id=a>', '<h2>', '</h2>', '<h6>', '</h6>', '<h7>', '</h7>',
  '<li>', '</li>', '<li', '<blockquote>', '</blockquote>', '<figcaption>', '</figcaption>',
  '<dt>', '</dt>', '<dd>', '</dd>',
  '<b>', '</b>', '<B>', '</B>', '<b ', '<strong>', '</strong>', '<strong',
  '<i>', '</i>', '<em>', '</em>', '<I>', '</EM>',
  '<a', '<a ', '<A ', '</a>', '</A>', '<a>', '<abbr ', '<a\n',
  'href', 'HREF', ' href', 'xhref', '-href', 'href ', '=', ' = ', '"', "'", '""', "''",
  'href="', "href='", 'href=""', 'href="/about"', "href='/x/y/'", 'href="#top"',
  'href="mailto:a@example.test"', 'href="javascript:alert(1)"',
  'href="https://studio.example/contact"', 'href="https://other.example/x"',
  'href="/a.jpg"', 'href="/login"', 'href="//x"', 'href="/x//"', 'href="about.html"',
  'href="/A?b#c"', 'href="sms:+1 (555) 555-0112"', 'href = "sms:5555550112"',
  '<br', '<br>', '<br/>', '<br />', '<BR / >', '<br  ', '<brx>', '<br clear="all">',
  '<!--', '-->', '--', '<!-->', '<!--->', '<!---->', '-',
  '<script', '<script>', '</script>', '<SCRIPT ', '</SCRIPT>', '<scripts ',
  '<script src="/a.js">', "<script src='/b.js?v=1'>", 'src=', 'SRC=', 'data-src=', ' src="', 'src=""',
  'src="https://img1.wsimg.com/x"', 'src="https://evil.example/x.js"',
  'src="data:text/javascript,1"', 'src="/w/s.js',
  '<style>', '</style>', '<noscript>', '</noscript>', '<template>', '</template>',
  '<iframe ', '</iframe>', '<svg>', '</svg>', '<head>', '</head>', '<header>', '</header>',
  '<nav>', '</nav>', '<NAV class=x>', '<footer>', '</footer>', '<aside>', '</aside>',
  '<title>', '</title>', '<TITLE lang=en>', '</TITLE>', '<title', '<titles>',
  '&amp;', '&lt;', '&gt;', '&quot;', '&apos;', '&nbsp;', '&mdash;', '&ndash;', '&hellip;',
  '&rsquo;', '&bogus;', '&', ';', '&#', '&#x', '&#x41;', '&#65;', '&#x110000;',
  '&#99999999999;', '&#xZZ;', '&amp', '&#x41',
  'x', 'a', 'Hello', 'world', 'Tea & cake', '1', '0', '12345', '#', '*', '_', '[', ']',
  '(', ')', '\\', '?', '.', '..', '/', '//', '///', ':',
  'http://', 'https://', 'HTTP://', 'HttpS://', 'http:/', 'ttp://', 'cdn.example.com/a/p',
  '.jpg', '.jpeg', '.jpe', '.JPG', '.png', '.webp', '.gif', '.jp', '.pngx', '.pdf',
  '?w=600', '.jpg?', 'a.b',
  'youtube.com/watch?v=', 'youtube.com/embed/', 'youtu.be/', 'AbC123xYz_-', 'abc',
  'vimeo.com/', 'vimeo.com/video/', '123456789', '1234567890123',
  'sms:', '+1-555-555-0112', 'javascript:', 'data:', 'mailto:', 'tel:',
  fromCode(0x17f), fromCode(0x212a), fromCode(0x130), fromCode(0x131), EM_DASH,
];

// Larger well-formed pieces, so that results are often not empty.
const PHRASES = [
  '<a href="/about">About</a>', "<a class='x' href='/services/'>Our <b>services</b></a>",
  '<a href="/contact" title="a>b">Contact</a>', '<a data-x="1" href="/team" href="/crew">Team</a>',
  '<a href="https://studio.example/blog/">Blog</a>', '<a href="/Pricing?x=1#y">Pricing</a>',
  '<a href="mailto:a@example.test">mail</a>', '<a href="/files/menu.pdf">menu</a>',
  '<a href="/wp-admin/">admin</a>', '<a href = "/spaced" >S</a>',
  '<a href="sms:+1-555-555-0112" class="cta">TEXT ME</a>', '<A HREF="/UPPER">U</A>',
  '<a href="/x"><i>it</i></a>',
  '<p>Plain paragraph.</p>',
  '<p>This is <strong>bold</strong> and <em>italic</em> with a <a href="/about">link</a>.</p>',
  '<p class="lead">Lead &amp; more&mdash;text.</p>', '<p>Line one<br>line two<br />three</p>',
  '<p>  spaced   out  </p>', '<p></p>',
  '<h1>Studio Name</h1>', '<h1 class="t">Title <script>alert(1)</script>End</h1>',
  '<h2>What I offer</h2>', '<h3>Deep</h3>',
  '<ul><li>Initial session</li><li>Follow-up</li></ul>', '<blockquote>"A gentle space."</blockquote>',
  '<dl><dt>Term</dt><dd>Def</dd></dl>', '<figure><figcaption>Caption</figcaption></figure>',
  '<title>Sample Practice</title>', '<title lang="en">A &amp; B</title>',
  '<head><title>In head</title></head>',
  '<nav><p>nav text</p><a href="/navlink">n</a></nav>', '<footer><p>footer text</p></footer>',
  '<aside><p>aside</p></aside>', '<header class="hero"><h1>Hero</h1><p>Intro.</p></header>',
  '<main>', '</main>', '<div>', '</div>', '<html><body>', '</body></html>',
  '<script>var a = "<p>not content</p>";</script>', '<script src="/widgets/script.js"></script>',
  "<script async src='/gallery/s.js?v=2'></script>",
  '<script src="https://img1.wsimg.com/blobby/go/site/script.js"></script>',
  '<script src="https://evil.example/x.js"></script>',
  '<script type="x" data-src="/lazy.js" defer>', '<style>p{color:red}</style>',
  '<noscript><p>enable js</p></noscript>', '<svg><title>icon</title></svg>',
  '<!-- a comment -->', '<!-- <p>hidden</p> -->', '<!--[if IE]><p>ie</p><![endif]-->',
  'https://cdn.example.com/a/photo1.jpg', 'https://img.wsimg.com/blobby/go/abc/p2.png?w=600&h=2',
  'http://x.example/a.b.c.webp)', "'https://x.example/g.gif'", 'https://x.example/page',
  'https://x.example/doc.pdf', 'https://a.example/1.jpeg?x=(1)',
  'https://a.example/http://b.example/2.JPG',
  '<img src="https://studio.example/static/visible.jpg">',
  'https://www.youtube.com/watch?v=AbC123xYz_-', 'https://www.youtube.com/embed/QwErTy123456',
  'https://youtu.be/ZxCvBn987654', 'see vimeo.com/video/123456789 now', 'vimeo.com/1234567',
  '/about/', '/Services/Appliances/', '/foo?x=1', '/foo#bar', 'about', '////', '/a//', '/a/?',
  '?x\n/y?z', '#',
];

function generate(rand: () => number, phraseShare: number, maxPieces: number): string {
  const count = 1 + Math.floor(rand() * maxPieces);
  let out = '';
  for (let k = 0; k < count; k += 1) {
    const pool = rand() < phraseShare ? PHRASES : TOKENS;
    out += pool[Math.floor(rand() * pool.length)];
  }
  return out;
}

describe('scanners return what the reference expressions return on generated input', () => {
  const SEED = 20260711;
  const CASES = 24000;

  it.each(COMPARED)('%s', (name, actual, reference) => {
    const rand = mulberry32(SEED);
    let nonEmpty = 0;
    for (let c = 0; c < CASES; c += 1) {
      const input = generate(rand, [0, 0.3, 0.6, 0.9][c % 4], 24);
      const expected = JSON.stringify(reference(input) ?? null);
      const got = JSON.stringify(actual(input) ?? null);
      if (got !== expected) {
        // One readable failure, with the input needed to reproduce it.
        expect({ fn: name, input, result: got }).toEqual({ fn: name, input, result: expected });
      }
      if (expected !== 'null' && expected !== '[]') nonEmpty += 1;
    }
    // The comparison means something only if the reference often has
    // something to say about these inputs.
    expect(nonEmpty).toBeGreaterThan(CASES / 5);
  });
});
