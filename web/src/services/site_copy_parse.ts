/**
 * Faithful-copy PARSE stage: every step of the crawl that reads the bytes a
 * remote site sent. `site_copy_crawl.ts` fetches a page and hands the text to
 * this file, by way of a worker thread (`site_copy_parse_worker.ts`).
 *
 * The input is hostile. The URL is one an operator pasted from a prospect, the
 * page may have been written to hurt whoever reads it, and it can be as large
 * as the crawler's body cap (2 MiB by default). The rule for this file is
 * therefore: THE WORK DONE ON A PAGE IS PROPORTIONAL TO ITS LENGTH. A pattern
 * that looks innocent, such as "an opening tag, anything, then its closing
 * tag", takes time proportional to the square of the input when the closing
 * tag never comes, because a backtracking matcher rescans the rest of the page
 * from every opening tag. Written that way, 320 KB of one repeated unclosed
 * tag takes seconds and a full-size page takes minutes.
 *
 * How the rule is kept
 * ====================
 *  - Tags, comments, anchors, script sources and media URLs are found by
 *    hand-written scanners that move forward only. Each one states, above its
 *    definition, the regular expression whose matches it reproduces, so the
 *    extraction behaviour is exactly what that expression defined.
 *  - What makes a scanner linear is `ForwardSearch`: a memo for "where is the
 *    next X at or after here?". When a terminator is missing, that is learned
 *    once, not once per opening tag.
 *  - The regular expressions that remain fall into three shapes, each of which
 *    is linear by construction: anchored at the start of the string (one
 *    attempt); no unbounded repetition (constant work per position); or one
 *    repeated character class that cannot contain the character an attempt
 *    starts with (so no two attempts scan the same text). Each carries a note
 *    saying which.
 *
 * Before adding a pattern here: no lazy "anything up to a terminator", no two
 * adjacent repetitions that can match the same characters, no repetition
 * followed by `$` on text that is not anchored at the start. Add the hostile
 * input you thought of to
 * `tests/unit/services.site-copy-crawl.linear-time.test.ts`; its timing cases
 * fail on a pattern that is not linear. A change to what a scanner matches
 * shows up in `services.site-copy-crawl.reference.test.ts`. The worker thread
 * and its wall-clock kill in `site_copy_crawl.ts` are the backstop for a
 * mistake that gets past both.
 *
 * This file imports nothing at run time, so the worker that loads it holds no
 * network or database code.
 *
 * Brand-voice em-dash carve-out: text returned from here is verbatim customer
 * copy. Em dashes are preserved deliberately; callers must not pass the
 * markdown blocks through an em-dash sanitizer.
 */

import type { CopyPage } from './harbor_to_compose';

export const MAX_BLOCKS_PER_PAGE = 60;
export const MAX_BLOCK_LENGTH = 8000;
export const MAX_MEDIA_CANDIDATES = 40;

// Site-builder CDN hosts whose script blobs carry lazy-loaded gallery /
// video data: a page from such a builder lists its images in a script, not in
// img tags. These are technical CDN hostnames for a server-side allowlist,
// never customer-facing copy. Tested against a parsed URL's hostname,
// anchored at its end.
// brand-voice-allow-competitor: server-side CDN-host allowlist for the faithful-copy crawler, not customer-facing comparison copy
const SCRIPT_BLOB_HOST_ALLOWLIST = /(^|\.)wsimg\.com$|(^|\.)squarespace\.com$|(^|\.)wixstatic\.com$/i;

// ============================================================
// Jobs: what the worker thread is asked to do
// ============================================================

export type ParseJob =
  | {
      kind: 'page';
      html: string;
      /** Normalized path of the page, copied into the CopyPage. */
      path: string;
      /** Origin of the crawl, for same-origin link and script decisions. */
      origin: string;
      /** Only the start page's links are followed. */
      discoverLinks: boolean;
    }
  | { kind: 'media'; text: string };

export interface PageParseResult {
  kind: 'page';
  /** Same-origin paths the page links to; null when the job did not ask. */
  links: string[] | null;
  mediaUrls: string[];
  scriptUrls: string[];
  page: CopyPage | null;
}

export interface MediaParseResult {
  kind: 'media';
  mediaUrls: string[];
}

export type ParseResult = PageParseResult | MediaParseResult;

/** Everything the crawl needs from one fetched body, in one call. */
export function runParseJob(job: ParseJob): ParseResult {
  if (job.kind === 'media') {
    return { kind: 'media', mediaUrls: extractMediaUrls(job.text) };
  }
  return {
    kind: 'page',
    links: job.discoverLinks ? extractInternalLinks(job.html, job.origin) : null,
    mediaUrls: extractMediaUrls(job.html),
    scriptUrls: extractScriptBlobUrls(job.html, job.origin),
    page: segmentHtmlToCopyPage(job.path, job.html),
  };
}

// ============================================================
// Scanning primitives
// ============================================================

const CODE_DOUBLE_QUOTE = 34;
const CODE_SINGLE_QUOTE = 39;
const CODE_SLASH = 47;
const CODE_EQUALS = 61;
const CODE_GREATER_THAN = 62;
const CODE_QUESTION_MARK = 63;

/** `\w` as a pattern without the unicode flag sees it: ASCII letters, digits, underscore. */
function isWordCode(code: number): boolean {
  return (
    (code >= 97 && code <= 122) ||
    (code >= 65 && code <= 90) ||
    (code >= 48 && code <= 57) ||
    code === 95
  );
}

/** `\s` as JavaScript patterns define it. */
function isSpaceCode(code: number): boolean {
  if (code <= 32) return code === 32 || (code >= 9 && code <= 13);
  if (code < 0xa0) return false;
  return (
    code === 0xa0 ||
    code === 0x1680 ||
    (code >= 0x2000 && code <= 0x200a) ||
    code === 0x2028 ||
    code === 0x2029 ||
    code === 0x202f ||
    code === 0x205f ||
    code === 0x3000 ||
    code === 0xfeff
  );
}

function isQuoteCode(code: number): boolean {
  return code === CODE_DOUBLE_QUOTE || code === CODE_SINGLE_QUOTE;
}

/** Index of the first character at or after `from` that is not whitespace. */
function skipSpace(text: string, from: number): number {
  let at = from;
  while (at < text.length && isSpaceCode(text.charCodeAt(at))) at += 1;
  return at;
}

/**
 * True when `text` holds `lowerAscii` at `at`, ignoring the case of ASCII
 * letters only. That is what the `i` flag means for an ASCII pattern without
 * the unicode flag: no character outside ASCII matches an ASCII letter.
 */
function matchesAt(text: string, at: number, lowerAscii: string): boolean {
  if (at < 0 || at + lowerAscii.length > text.length) return false;
  for (let offset = 0; offset < lowerAscii.length; offset += 1) {
    let code = text.charCodeAt(at + offset);
    if (code >= 65 && code <= 90) code += 32;
    if (code !== lowerAscii.charCodeAt(offset)) return false;
  }
  return true;
}

/**
 * "Where is the next X at or after `from`?", for a caller whose questions
 * never move backwards. The last answer is remembered: a question that falls
 * between the previous question and its answer has the same answer, and once
 * the answer is "nowhere" it stays "nowhere". Stretches of text are therefore
 * searched once each, however many questions land in them, which is what
 * keeps a scanner linear when a terminator is far away or missing.
 *
 * `find` returns an index at or after `from`, or -1.
 */
class ForwardSearch {
  private askedFrom = 0;
  private answer = 0;
  private asked = false;

  constructor(private readonly find: (from: number) => number) {}

  next(from: number): number {
    if (this.asked && from >= this.askedFrom && (this.answer === -1 || from <= this.answer)) {
      return this.answer;
    }
    this.asked = true;
    this.askedFrom = from;
    this.answer = this.find(from);
    return this.answer;
  }
}

function nextGreaterThan(html: string): ForwardSearch {
  return new ForwardSearch((from) => html.indexOf('>', from));
}

function nextQuote(html: string): ForwardSearch {
  return new ForwardSearch((from) => {
    for (let at = from; at < html.length; at += 1) {
      if (isQuoteCode(html.charCodeAt(at))) return at;
    }
    return -1;
  });
}

/** Next `</name>` (ASCII case-insensitive, nothing between the name and `>`). */
function nextClosingTag(html: string, name: string): ForwardSearch {
  return new ForwardSearch((from) => {
    let at = html.indexOf('</', from);
    while (at !== -1) {
      if (
        matchesAt(html, at + 2, name) &&
        html.charCodeAt(at + 2 + name.length) === CODE_GREATER_THAN
      ) {
        return at;
      }
      at = html.indexOf('</', at + 2);
    }
    return -1;
  });
}

/**
 * The tag name that starts at `at`, if it is one of `names` and ends there
 * (`<p` followed by a non-word character is `p`; `<pre` is not).
 */
function tagNameAt(html: string, at: number, names: readonly string[]): string | null {
  // Setting bit 5 lower-cases an ASCII letter. It is only a cheap first
  // filter: `matchesAt` makes the real comparison.
  const first = html.charCodeAt(at) | 0x20;
  for (const name of names) {
    if (
      name.charCodeAt(0) === first &&
      matchesAt(html, at, name) &&
      !isWordCode(html.charCodeAt(at + name.length))
    ) {
      return name;
    }
  }
  return null;
}

interface ElementSpan {
  /** Lower-case tag name. */
  tag: string;
  /** Index of the opening `<`. */
  start: number;
  /** Index just past the closing tag. */
  end: number;
  innerStart: number;
  innerEnd: number;
}

/**
 * The matches of `/<(NAMES)\b[^>]*>([\s\S]*?)<\/\1>/gi`, left to right and
 * without overlap, for the given tag names, starting the search at `from`.
 * An element runs from its opening tag to the FIRST closing tag of the same
 * name; nesting is not tracked, exactly as with that expression.
 *
 * Linear: an opening tag with no `>` anywhere after it ends the scan (no
 * later tag has one either), and a name whose closing tag is missing is never
 * looked for again.
 */
function* elementSpans(
  html: string,
  names: readonly string[],
  from = 0
): Generator<ElementSpan, void, undefined> {
  const tagEnd = nextGreaterThan(html);
  const closers = new Map<string, ForwardSearch>();
  const unclosed = new Set<string>();
  let position = from;
  for (;;) {
    const start = html.indexOf('<', position);
    if (start === -1) return;
    const tag = tagNameAt(html, start + 1, names);
    if (tag === null || unclosed.has(tag)) {
      position = start + 1;
      continue;
    }
    const openEnd = tagEnd.next(start + 1 + tag.length);
    if (openEnd === -1) return;
    let closer = closers.get(tag);
    if (!closer) {
      closer = nextClosingTag(html, tag);
      closers.set(tag, closer);
    }
    const close = closer.next(openEnd + 1);
    if (close === -1) {
      unclosed.add(tag);
      position = start + 1;
      continue;
    }
    const end = close + tag.length + 3;
    yield { tag, start, end, innerStart: openEnd + 1, innerEnd: close };
    position = end;
  }
}

/** `html.replace(/<(NAMES)\b[^>]*>([\s\S]*?)<\/\1>/gi, ...)`, with the replacement computed from the inner HTML. */
function replaceElements(
  html: string,
  names: readonly string[],
  replacement: (innerHtml: string) => string
): string {
  const parts: string[] = [];
  let copied = 0;
  for (const span of elementSpans(html, names)) {
    parts.push(html.slice(copied, span.start), replacement(html.slice(span.innerStart, span.innerEnd)));
    copied = span.end;
  }
  if (parts.length === 0) return html;
  parts.push(html.slice(copied));
  return parts.join('');
}

const _ANCHOR_TAG: readonly string[] = ['a'];

interface AnchorMatch {
  /** Index of the opening `<`. */
  start: number;
  /** Index just past `</a>`. */
  end: number;
  /** The quoted href value, exactly as written. */
  href: string;
  innerStart: number;
  innerEnd: number;
}

/**
 * The matches of
 * `/<a\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi`,
 * left to right and without overlap.
 *
 * Within one opening tag that expression takes the LAST quoted, non-empty
 * href before the tag's first `>` for which the rest still matches (a `>`
 * after the value, then a `</a>`). Whether an href qualifies does not depend
 * on which `<a` it is reached from, and the three things it needs (a closing
 * quote, a `>`, a `</a>`) can only get scarcer further right. So the
 * candidates are walked once, left to right; the first time one of the three
 * is missing, nothing further right can match and the scan ends.
 *
 * Linear: the text before a tag's first `>` is examined once, for the first
 * `<a` that reaches it. If that tag has no usable href, no `<a` inside the
 * same stretch has one either, and the scan resumes at the `>`.
 */
function* anchorMatches(html: string): Generator<AnchorMatch, void, undefined> {
  const greaterThan = nextGreaterThan(html);
  const quote = nextQuote(html);
  const closingAnchor = nextClosingTag(html, 'a');
  let position = 0;
  for (;;) {
    const start = html.indexOf('<', position);
    if (start === -1) return;
    if (tagNameAt(html, start + 1, _ANCHOR_TAG) === null) {
      position = start + 1;
      continue;
    }
    const firstTagEnd = greaterThan.next(start + 2);
    const prefixEnd = firstTagEnd === -1 ? html.length : firstTagEnd;

    let best: AnchorMatch | null = null;
    let exhausted = false;
    for (let at = start + 2; at + 4 <= prefixEnd; at += 1) {
      // `\bhref`: the word "href" with a non-word character before it.
      if (!matchesAt(html, at, 'href') || isWordCode(html.charCodeAt(at - 1))) continue;
      let cursor = skipSpace(html, at + 4);
      if (html.charCodeAt(cursor) !== CODE_EQUALS) continue;
      cursor = skipSpace(html, cursor + 1);
      if (!isQuoteCode(html.charCodeAt(cursor))) continue;
      const valueStart = cursor + 1;
      const valueEnd = quote.next(valueStart);
      if (valueEnd === -1) {
        exhausted = true;
        break;
      }
      if (valueEnd === valueStart) continue; // href="" is not a link
      const openEnd = greaterThan.next(valueEnd + 1);
      const close = openEnd === -1 ? -1 : closingAnchor.next(openEnd + 1);
      if (close === -1) {
        exhausted = true;
        break;
      }
      best = {
        start,
        end: close + 4,
        href: html.slice(valueStart, valueEnd),
        innerStart: openEnd + 1,
        innerEnd: close,
      };
    }

    if (best) {
      yield best;
      position = best.end;
    } else {
      position = prefixEnd;
    }
    if (exhausted || firstTagEnd === -1) return;
  }
}

/**
 * The captured values of `/<script[^>]+src=["']([^"']+)["']/gi`, left to
 * right. As with that expression: the tag name is not delimited (`<scripts`
 * counts), "src=" is not delimited either (`data-src=` counts), the quotes
 * need not be the same kind, and within one tag the LAST quoted, non-empty
 * value before the tag's first `>` wins.
 *
 * Linear for the same reason as `anchorMatches`: the text before a tag's
 * first `>` is examined once.
 */
function* scriptSrcValues(html: string): Generator<string, void, undefined> {
  const greaterThan = nextGreaterThan(html);
  const quote = nextQuote(html);
  let position = 0;
  for (;;) {
    const start = html.indexOf('<', position);
    if (start === -1) return;
    if (!matchesAt(html, start + 1, 'script')) {
      position = start + 1;
      continue;
    }
    const nameEnd = start + 7;
    const firstTagEnd = greaterThan.next(nameEnd);
    const prefixEnd = firstTagEnd === -1 ? html.length : firstTagEnd;

    let valueStart = -1;
    let valueEnd = -1;
    let exhausted = false;
    // `[^>]+` needs one character between the tag name and "src=".
    for (let at = nameEnd + 1; at + 4 <= prefixEnd; at += 1) {
      if (!matchesAt(html, at, 'src=') || !isQuoteCode(html.charCodeAt(at + 4))) continue;
      const closingQuote = quote.next(at + 5);
      if (closingQuote === -1) {
        exhausted = true;
        break;
      }
      if (closingQuote === at + 5) continue; // src="" names nothing
      valueStart = at + 5;
      valueEnd = closingQuote;
    }

    if (valueStart !== -1) {
      yield html.slice(valueStart, valueEnd);
      // Nothing between this value and the tag's `>` can match (this was the
      // last value that does), so resume past both.
      position = Math.max(valueEnd + 1, prefixEnd);
    } else {
      position = prefixEnd;
    }
    if (exhausted || firstTagEnd === -1) return;
  }
}

// ============================================================
// Deep-media mining helpers
// ============================================================

/** Same-origin or allowlisted-CDN <script src> URLs found in a page. */
export function extractScriptBlobUrls(html: string, origin: string): string[] {
  const out: string[] = [];
  for (const raw of scriptSrcValues(html)) {
    let abs: URL;
    try {
      abs = new URL(raw, origin);
    } catch {
      continue;
    }
    if (abs.protocol !== 'https:' && abs.protocol !== 'http:') continue;
    const sameOrigin = abs.origin === origin;
    const allowlisted = SCRIPT_BLOB_HOST_ALLOWLIST.test(abs.hostname);
    if (!sameOrigin && !allowlisted) continue;
    // No unbounded repetition: constant work per position.
    if (!/\.js(\?|$)/i.test(abs.pathname + abs.search) && !allowlisted) continue;
    out.push(abs.toString());
  }
  return out;
}

/** A character `[^\s"'\\()<>]` accepts: what may appear inside a media URL. */
function isUrlBodyCode(code: number): boolean {
  return !(
    isSpaceCode(code) ||
    code === CODE_DOUBLE_QUOTE ||
    code === CODE_SINGLE_QUOTE ||
    code === 92 || // backslash
    code === 40 || // (
    code === 41 || // )
    code === 60 || // <
    code === CODE_GREATER_THAN
  );
}

/** Length of the image extension `(?:jpe?g|png|webp|gif)` at `at`, or 0. */
function imageExtensionLength(text: string, at: number): number {
  if (matchesAt(text, at, 'jp')) {
    if (matchesAt(text, at + 2, 'eg')) return 4;
    return matchesAt(text, at + 2, 'g') ? 3 : 0;
  }
  if (matchesAt(text, at, 'png')) return 3;
  if (matchesAt(text, at, 'webp')) return 4;
  if (matchesAt(text, at, 'gif')) return 3;
  return 0;
}

/**
 * The matches of
 * `/https?:\/\/[^\s"'\\()<>]+?\.(?:jpe?g|png|webp|gif)(?:\?[^\s"'\\()<>]*)?/gi`,
 * left to right: an absolute URL up to its FIRST image extension, plus a
 * query string when one follows directly.
 *
 * Linear: a run of URL characters is measured once and searched for an
 * extension once, however many "http://" it contains. When the run holds no
 * extension, no URL starting inside it can match, and the scan resumes at the
 * end of the run.
 */
function* imageUrlMatches(text: string): Generator<string, void, undefined> {
  // A literal with one optional character: constant work per position.
  const urlStart = /https?:\/\//gi;
  const runEnd = new ForwardSearch((from) => {
    let at = from;
    while (at < text.length && isUrlBodyCode(text.charCodeAt(at))) at += 1;
    return at;
  });
  const extensionDot = new ForwardSearch((from) => {
    let at = text.indexOf('.', from);
    while (at !== -1 && imageExtensionLength(text, at + 1) === 0) {
      at = text.indexOf('.', at + 1);
    }
    return at;
  });
  for (;;) {
    const match = urlStart.exec(text);
    if (match === null) return;
    const bodyStart = match.index + match[0].length;
    const bodyEnd = runEnd.next(bodyStart);
    // The lazy `+?` needs one URL character before the dot.
    const dot = extensionDot.next(bodyStart + 1);
    if (dot === -1) return;
    if (dot >= bodyEnd) {
      urlStart.lastIndex = bodyEnd;
      continue;
    }
    let end = dot + 1 + imageExtensionLength(text, dot + 1);
    if (text.charCodeAt(end) === CODE_QUESTION_MARK) end = bodyEnd;
    yield text.slice(match.index, end);
    urlStart.lastIndex = end;
  }
}

/**
 * Media URLs + canonical video URLs mined from arbitrary text (HTML or
 * a JS blob). Images: absolute http(s) URLs ending in a raster
 * extension (query strings tolerated). Videos: YouTube watch/embed/
 * short forms + numeric Vimeo IDs, normalized to canonical URLs.
 */
export function extractMediaUrls(text: string): string[] {
  const out = new Set<string>();
  for (const url of imageUrlMatches(text)) {
    out.add(url);
    if (out.size >= MAX_MEDIA_CANDIDATES) return [...out];
  }
  let m: RegExpExecArray | null;
  // Both video patterns repeat a class a bounded number of times ({6,20},
  // {6,12}): constant work per position.
  const yt = /(?:youtube\.com\/(?:watch\?v=|embed\/)|youtu\.be\/)([A-Za-z0-9_-]{6,20})/g;
  while ((m = yt.exec(text)) !== null) {
    out.add(`https://www.youtube.com/watch?v=${m[1]}`);
    if (out.size >= MAX_MEDIA_CANDIDATES) return [...out];
  }
  const vimeo = /vimeo\.com\/(?:video\/)?(\d{6,12})/g;
  while ((m = vimeo.exec(text)) !== null) {
    out.add(`https://vimeo.com/${m[1]}`);
    if (out.size >= MAX_MEDIA_CANDIDATES) return [...out];
  }
  return [...out];
}

// ============================================================
// HTML → CopyPage segmenter
// ============================================================

export function segmentHtmlToCopyPage(
  path: string,
  html: string
): CopyPage | null {
  const stripped = stripNonContent(html);
  const title = extractTitle(html);
  const blocks = extractBlocks(stripped);
  if (blocks.length === 0 && !title) return null;

  const hero = extractHero(stripped, html);
  const entry: CopyPage = {
    path,
    markdown_blocks: blocks.slice(0, MAX_BLOCKS_PER_PAGE),
  };
  if (title) entry.title = title.slice(0, 200);
  if (hero) entry.hero = hero;
  return entry;
}

// We strip site-level chrome before block extraction. <header> is
// deliberately NOT in this list because many ICP sites (Squarespace,
// hand-rolled solo-practitioner sites) put the page H1 + hero copy
// inside <header class="site-header"> or <header class="hero">.
// Stripping <header> would silently produce hero-less imports on
// exactly the customer profile we target.
const _NON_CONTENT_TAGS = [
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

/**
 * Replaces each non-content element, then each comment, with one space. One
 * tag name per pass, in the order listed, so what an earlier pass removed can
 * no longer open or close an element for a later one.
 */
function stripNonContent(html: string): string {
  let out = html;
  for (const tag of _NON_CONTENT_TAGS) {
    out = replaceElements(out, [tag], () => ' ');
  }
  return stripComments(out);
}

/**
 * `html.replace(/<!--[\s\S]*?-->/g, ' ')`. Linear: when a `<!--` has no `-->`
 * after it, no later one has either, and the scan ends.
 */
function stripComments(html: string): string {
  const parts: string[] = [];
  let copied = 0;
  for (;;) {
    const open = html.indexOf('<!--', copied);
    if (open === -1) break;
    const close = html.indexOf('-->', open + 4);
    if (close === -1) break;
    parts.push(html.slice(copied, open), ' ');
    copied = close + 3;
  }
  if (parts.length === 0) return html;
  parts.push(html.slice(copied));
  return parts.join('');
}

const _TITLE_TAG: readonly string[] = ['title'];

function extractTitle(html: string): string | undefined {
  const first = elementSpans(html, _TITLE_TAG).next();
  if (first.done) return undefined;
  const span = first.value;
  const t = decodeHtml(stripTags(html.slice(span.innerStart, span.innerEnd))).trim();
  return t || undefined;
}

const _BLOCK_TAGS: readonly string[] = [
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'p',
  'li',
  'blockquote',
  'figcaption',
  'dt',
  'dd',
];

function extractBlocks(html: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const span of elementSpans(html, _BLOCK_TAGS)) {
    const md = htmlToMarkdownBlock(span.tag, html.slice(span.innerStart, span.innerEnd));
    if (!md) continue;
    if (md.length > MAX_BLOCK_LENGTH) continue;
    // Dedupe identical blocks (common from boilerplate headers/footers
    // that sneak past the strip step).
    const key = md;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(md);
    if (out.length >= MAX_BLOCKS_PER_PAGE) break;
  }
  return out;
}

function htmlToMarkdownBlock(tag: string, innerHtml: string): string | null {
  const text = renderInlineHtmlToMarkdown(innerHtml);
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (/^h([1-6])$/.test(tag)) {
    const level = Number(tag.slice(1));
    return `${'#'.repeat(level)} ${trimmed}`;
  }
  if (tag === 'li') return `- ${trimmed}`;
  if (tag === 'blockquote') return `> ${trimmed}`;
  // p, figcaption, dt, dd → plain paragraph
  return trimmed;
}

const _BOLD_TAGS: readonly string[] = ['b', 'strong'];
const _ITALIC_TAGS: readonly string[] = ['i', 'em'];

function renderInlineHtmlToMarkdown(html: string): string {
  // Replace <br> with newlines first, before tag stripping.
  let s = replaceLineBreakTags(html);
  // Bold + strong
  s = replaceElements(s, _BOLD_TAGS, (inner) => `**${stripTags(inner).trim()}**`);
  // Italic + em
  s = replaceElements(s, _ITALIC_TAGS, (inner) => `*${stripTags(inner).trim()}*`);
  // Links → [text](href). Skip if href is empty / javascript:.
  s = replaceAnchors(s);
  // Strip remaining tags. Decode entities last.
  s = stripTags(s);
  s = decodeHtml(s);
  // Collapse whitespace to single spaces, but keep \n for <br>, and collapse
  // 3+ newlines to 2. Each pattern repeats one class that excludes the
  // character an attempt starts with, so no text is scanned twice.
  s = s.replace(/[ \t\r\f]+/g, ' ').replace(/\n[ \t]+/g, '\n');
  s = s.replace(/\n{3,}/g, '\n\n');
  return s;
}

/**
 * `html.replace(/<br\s*\/?\s*>/gi, '\n')`. Written out because the two `\s*`
 * in that expression can share one run of whitespace when the slash is
 * absent, and a backtracking matcher then tries every way of splitting it.
 */
function replaceLineBreakTags(html: string): string {
  const parts: string[] = [];
  let copied = 0;
  let position = 0;
  for (;;) {
    const start = html.indexOf('<', position);
    if (start === -1) break;
    position = start + 1;
    if (!matchesAt(html, start + 1, 'br')) continue;
    let cursor = skipSpace(html, start + 3);
    if (html.charCodeAt(cursor) === CODE_SLASH) cursor = skipSpace(html, cursor + 1);
    if (html.charCodeAt(cursor) !== CODE_GREATER_THAN) continue;
    parts.push(html.slice(copied, start), '\n');
    copied = cursor + 1;
    position = copied;
  }
  if (parts.length === 0) return html;
  parts.push(html.slice(copied));
  return parts.join('');
}

function replaceAnchors(html: string): string {
  const parts: string[] = [];
  let copied = 0;
  for (const anchor of anchorMatches(html)) {
    const text = stripTags(html.slice(anchor.innerStart, anchor.innerEnd)).trim();
    const safe = sanitizeHref(anchor.href);
    const rendered = !safe ? text : !text ? safe : `[${text}](${safe})`;
    parts.push(html.slice(copied, anchor.start), rendered);
    copied = anchor.end;
  }
  if (parts.length === 0) return html;
  parts.push(html.slice(copied));
  return parts.join('');
}

/**
 * `html.replace(/<[^>]+>/g, '')`: removes everything from a `<` to the next
 * `>`, when at least one character lies between them. Linear: a `<` with no
 * `>` after it ends the scan, since no later `<` has one either.
 */
function stripTags(html: string): string {
  const parts: string[] = [];
  let copied = 0;
  let position = 0;
  for (;;) {
    const start = html.indexOf('<', position);
    if (start === -1) break;
    const end = html.indexOf('>', start + 1);
    if (end === -1) break;
    if (end === start + 1) {
      // "<>" has nothing inside and stays as text.
      position = end + 1;
      continue;
    }
    parts.push(html.slice(copied, start));
    copied = end + 1;
    position = copied;
  }
  if (parts.length === 0) return html;
  parts.push(html.slice(copied));
  return parts.join('');
}

const _ENTITY_MAP: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
  // Em dash + en dash + curly quotes are preserved as their unicode
  // equivalents because they are CUSTOMER-AUTHORED copy and the crawler is
  // explicitly carved out from the no-em-dash brand-voice rule. Do not
  // map mdash to a hyphen here.
  // brand-voice-allow-em-dash: html entity decode for customer copy carve-out
  mdash: String.fromCharCode(0x2014),
  ndash: '–',
  hellip: '…',
  lsquo: '‘',
  rsquo: '’',
  ldquo: '“',
  rdquo: '”',
};

// String.fromCodePoint throws a RangeError above U+10FFFF, so a page containing
// "&#x110000;" or "&#99999999999;" would otherwise abort the whole crawl.
// Out-of-range references are left as literal text, like an unknown entity.
function codePointToString(codePoint: number, original: string): string {
  if (!Number.isInteger(codePoint) || codePoint < 0 || codePoint > 0x10ffff) return original;
  return String.fromCodePoint(codePoint);
}

// Each pattern starts at "&" and repeats one class that cannot contain "&",
// so an attempt that fails (no ";") is never rescanned by another.
function decodeHtml(s: string): string {
  return s
    .replace(/&#x([0-9a-fA-F]+);/g, (m, hex) => codePointToString(parseInt(hex, 16), m))
    .replace(/&#(\d+);/g, (m, dec) => codePointToString(parseInt(dec, 10), m))
    .replace(/&([a-zA-Z]+);/g, (m, name) => _ENTITY_MAP[name] ?? m);
}

// Every pattern here is anchored at the start of the string: one attempt.
function sanitizeHref(href: string): string | null {
  const v = href.trim();
  if (!v) return null;
  if (/^javascript:/i.test(v)) return null;
  if (/^data:/i.test(v)) return null;
  // Common safe schemes + relative + absolute paths + same-page hash.
  if (/^(https?:|sms:|tel:|mailto:|\/|\.\.?\/|#)/i.test(v)) return v;
  // Bare relative without leading ./ (e.g. "about.html") → treat as relative.
  if (/^[a-z0-9_./?=&%-]+$/i.test(v)) return v;
  return null;
}

// ============================================================
// Internal-link discovery
// ============================================================

// Anchored at the start of the string: one attempt.
const _SAFE_LINK_PATH_RE = /^\/[a-z0-9/_-]*$/;
const _SKIP_PATH_FRAGMENTS = [
  '/wp-admin',
  '/wp-login',
  '/cart',
  '/checkout',
  '/account',
  '/login',
  '/signup',
];

export function extractInternalLinks(html: string, origin: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  // The strip-non-content pass removes nav and footer in the BLOCK
  // pipeline; for link discovery we want nav links too, so use the raw
  // HTML.
  for (const anchor of anchorMatches(html)) {
    const rawHref = anchor.href.trim();
    if (!rawHref) continue;
    if (rawHref.startsWith('#')) continue;
    if (/^(mailto:|tel:|sms:|javascript:|data:)/i.test(rawHref)) continue;
    let abs: URL;
    try {
      abs = new URL(rawHref, origin + '/');
    } catch {
      continue;
    }
    // Same-origin only.
    if (abs.origin !== origin) continue;
    const path = normalizePath(abs.pathname);
    if (path === '/') continue; // home will be added by caller
    if (!_SAFE_LINK_PATH_RE.test(path)) continue;
    if (_SKIP_PATH_FRAGMENTS.some((frag) => path.includes(frag))) continue;
    // Skip filenames that look like assets, not pages. No unbounded
    // repetition: constant work per position.
    if (/\.(jpe?g|png|gif|webp|svg|pdf|zip|css|js|ico|xml|json)$/i.test(path)) {
      continue;
    }
    if (seen.has(path)) continue;
    seen.add(path);
    out.push(path);
  }
  // Stable alphabetical order so output is deterministic.
  out.sort((a, b) => a.localeCompare(b));
  return out;
}

// ============================================================
// Hero detection
// ============================================================

const _H1_TAG: readonly string[] = ['h1'];
const _P_TAG: readonly string[] = ['p'];

function extractHero(
  contentHtml: string,
  rawHtml: string
): CopyPage['hero'] | undefined {
  const hero: NonNullable<CopyPage['hero']> = {};
  const firstH1 = elementSpans(contentHtml, _H1_TAG).next();
  const h1 = firstH1.done ? null : firstH1.value;
  if (h1) {
    // Heading is plain text; consumers HTML-escape end-to-end. Order:
    // strip inner tags, then decode entities. Without `stripTags`
    // first, an inner `&lt;img...&gt;` would decode back to a real
    // img tag and the downstream consumer would have to re-escape.
    const heading = decodeHtml(stripTags(contentHtml.slice(h1.innerStart, h1.innerEnd))).trim();
    if (heading) hero.heading = heading.slice(0, 400);
  }
  // Hero body: paragraphs immediately following the H1 (up to 2).
  if (h1) {
    const parts: string[] = [];
    for (const paragraph of elementSpans(contentHtml, _P_TAG, h1.end)) {
      const md = renderInlineHtmlToMarkdown(
        contentHtml.slice(paragraph.innerStart, paragraph.innerEnd)
      ).trim();
      if (!md) continue;
      // Escape every HTML-significant character in the inner text
      // before wrapping in a static <p>. The widget emits
      // body_html via `<%- %>` (raw) so we own the escape contract.
      // Escaping `<`, `>`, `&`, `"`, `'` covers all attribute and
      // element-context breakouts; the wrapper `<p>...</p>` is
      // static text we control.
      parts.push(`<p>${escapeHtmlText(md)}</p>`);
      if (parts.length === 2) break;
    }
    if (parts.length > 0) {
      hero.body_html = parts.join('\n').slice(0, MAX_BLOCK_LENGTH);
    }
  }
  // SMS link: scan raw HTML (not stripped) for href="sms:...". Linear: an
  // attempt starts at the letters "href" and everything it can then scan
  // (whitespace, "=", a quote, "sms:", then digits, whitespace and
  // punctuation) contains no letter "h", so no other attempt starts inside it.
  const sms = rawHtml.match(/href\s*=\s*["']sms:([+\d\s\-()]+)["']/i);
  if (sms) {
    const digits = sms[1].replace(/\D+/g, '');
    if (digits.length >= 7 && digits.length <= 15) {
      hero.cta_text_me_phone = digits;
    }
  }
  return Object.keys(hero).length > 0 ? hero : undefined;
}

function escapeHtmlText(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ============================================================
// Path helper
// ============================================================

function isLineTerminatorCode(code: number): boolean {
  return code === 10 || code === 13 || code === 0x2028 || code === 0x2029;
}

/**
 * Lower-cases a path, drops its query and fragment, gives it a leading slash
 * and removes trailing slashes. The results are those of `/[?#].*$/` followed
 * by `/\/+$/`; it is written as loops because both expressions are quadratic
 * on a long run of slashes or question marks that does not reach the end of
 * the string (every position in the run starts a scan to the end).
 */
export function normalizePath(p: string): string {
  let out = (p || '/').toLowerCase();
  // `/[?#].*$/`: the first "?" or "#" after which no line terminator
  // follows (`.` does not cross one) starts the part to drop.
  let afterLastLineBreak = out.length;
  while (afterLastLineBreak > 0 && !isLineTerminatorCode(out.charCodeAt(afterLastLineBreak - 1))) {
    afterLastLineBreak -= 1;
  }
  for (let at = afterLastLineBreak; at < out.length; at += 1) {
    const code = out.charCodeAt(at);
    if (code === CODE_QUESTION_MARK || code === 35) {
      out = out.slice(0, at);
      break;
    }
  }
  if (!out.startsWith('/')) out = `/${out}`;
  if (out.length > 1) {
    let end = out.length;
    while (end > 0 && out.charCodeAt(end - 1) === CODE_SLASH) end -= 1;
    out = out.slice(0, end);
  }
  return out;
}
