/**
 * Cleaning of an uploaded SVG logo: what `logo.ts` stores is not the upload
 * but a new document written here from the parts of the upload that are on a
 * list.
 *
 * The input is hostile. Any logged-in customer can upload up to 2 MB of it,
 * and an SVG that a browser opens as a document (not through `<img>`) is a
 * program: it can run script, load other files and submit links. A filter
 * that looks for the dangerous parts and deletes them has to know every way
 * of spelling each one; this file keeps a list of what a logo needs instead.
 *
 * What comes out
 * ==============
 *  - One fixed XML declaration (UTF-8), then one `svg` element whose namespace
 *    declarations are written here: the SVG namespace as the default and the
 *    XLink namespace as `xlink`. No other namespace declaration is copied, so
 *    no prefix or re-declared default can turn a listed name into something
 *    else.
 *  - Elements whose exact name is in `ALLOWED_ELEMENTS`: shapes, groups,
 *    text, gradients, patterns, clipping, masking, markers, filters, embedded
 *    raster images, `style`, `a`. Any other element is left out together with
 *    everything inside it. That covers script, foreignObject, every HTML
 *    element (iframe, embed, object, audio, video, canvas, link, meta), the
 *    animation elements (which can rewrite another element's attributes after
 *    the file is loaded) and every element written with a prefix.
 *  - Attributes whose exact name is in `ALLOWED_ATTRIBUTES`. Event handlers
 *    (`on...`) are not on it, nor is any prefixed attribute other than
 *    `xlink:href`, `xml:space` and `xml:lang`.
 *  - A reference (`href`, `xlink:href`) only when it stays inside the file
 *    (`#id`); on `image` and `feImage` also an embedded PNG, JPEG, GIF or WebP;
 *    on `a` also an `http:`, `https:` or `mailto:` link. Everything else,
 *    script URLs and other documents included, loses the attribute.
 *  - Style sheets and `style` attributes with every `@import` rule removed
 *    and every `url(...)` that is not `#id` or an embedded image or font
 *    replaced by `none` (`cleanCss`). Any other attribute whose value the same
 *    cleaner would have to change is left out.
 *  - Text, with markup characters escaped.
 * Never copied: the document type declaration (and with it every entity
 * definition; an entity reference other than the five built-in ones stays the
 * literal text it was written as), processing instructions, comments, and the
 * CDATA wrapper (its content is kept as ordinary text).
 *
 * Values are decoded before they are judged and encoded again when written,
 * so a rule cannot be dodged by spelling a character as a reference, and what
 * a reader of the stored file decodes is the string that was judged.
 *
 * A document this file cannot read as well-formed XML with an `svg` root is
 * refused whole (the result is the empty string), not repaired: a browser
 * would refuse it too, and guessing what a broken tag meant is how a filter
 * and a browser come to disagree.
 *
 * Time
 * ====
 * THE WORK DONE ON A DOCUMENT IS PROPORTIONAL TO ITS LENGTH. The reader moves
 * forward only: it finds the next "<", reads one token, and never returns to
 * a character it has passed. A terminator that is missing (an unclosed
 * comment, quote or tag) ends the run at once. The regular expressions here
 * are of two shapes, both linear by construction: a single character class
 * with no repetition around it, replaced globally, or a pattern with only
 * bounded repetition. Before adding one, read the note at the top of
 * `site_copy_parse.ts`, and add the hostile input you thought of to
 * `tests/unit/services.logo.linear-time.test.ts`.
 *
 * `logo.ts` still runs this in a worker thread with a wall-clock limit: the
 * proportionality above is the guarantee, the thread is what holds if a later
 * change breaks it. This file imports nothing, so that thread loads no
 * network, database or image code.
 *
 * Size and colors
 * ===============
 * An uploaded SVG is never drawn on the server: drawing is the one step whose
 * cost the length of the file does not bound (a long run of text, a filled
 * path of many curves or a chain of masks can take a drawing library minutes
 * or its stack). The two things the upload route used to learn by drawing are
 * read from the text instead, in the same pass (`cleanSvg`):
 *  - the size, from the root's `width`, `height` and `viewBox` (`rootSize`);
 *  - the colors, by counting how often each one is named in `fill`, `stroke`,
 *    `stop-color`, `flood-color`, `lighting-color` and `color`, as attributes
 *    and as declarations in style sheets and `style` attributes. Hex colors,
 *    `rgb()` and the names in `NAMED_COLORS` are understood; anything else
 *    (`currentColor`, `url(#gradient)`, `hsl()`) is not counted.
 * A count of mentions is not a measure of area: a color used once for a
 * large background ranks the same as one used once for a dot.
 */

/** Longest SVG text, in UTF-16 code units, that is read. */
export const MAX_SVG_LENGTH = 2 * 1024 * 1024;

/**
 * How much longer than its source a cleaned document may be. Writing the
 * fixed declaration and namespaces adds about 130 characters, and escaping a
 * character that the source carried raw (in a CDATA section, say) adds a
 * few each. Without a limit a source made of such characters would come out
 * several times its size.
 */
function longestOutputFor(sourceLength: number): number {
  return sourceLength + Math.floor(sourceLength / 8) + 1024;
}

/** Deepest element nesting that is read (libxml2, a common reader of these files, stops at the same depth). */
const MAX_ELEMENT_DEPTH = 256;

const XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n';
const ROOT_NAMESPACES =
  ' xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"';

const ALLOWED_ELEMENTS: ReadonlySet<string> = new Set([
  // Structure
  'svg', 'g', 'defs', 'symbol', 'use', 'switch', 'a', 'title', 'desc', 'style',
  // Shapes, text, images
  'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
  'text', 'tspan', 'textPath', 'image',
  // Paint servers, clipping, masking, markers
  'linearGradient', 'radialGradient', 'stop', 'pattern', 'clipPath', 'mask', 'marker',
  // Filters
  'filter', 'feBlend', 'feColorMatrix', 'feComponentTransfer', 'feComposite',
  'feConvolveMatrix', 'feDiffuseLighting', 'feDisplacementMap', 'feDistantLight',
  'feDropShadow', 'feFlood', 'feFuncA', 'feFuncB', 'feFuncG', 'feFuncR',
  'feGaussianBlur', 'feImage', 'feMerge', 'feMergeNode', 'feMorphology', 'feOffset',
  'fePointLight', 'feSpecularLighting', 'feSpotLight', 'feTile', 'feTurbulence',
]);

const ALLOWED_ATTRIBUTES: ReadonlySet<string> = new Set([
  // Identity, language, accessibility
  'id', 'class', 'style', 'lang', 'xml:lang', 'xml:space', 'role',
  'aria-label', 'aria-hidden', 'aria-labelledby', 'version',
  // References
  'href', 'xlink:href',
  // Geometry
  'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'dx', 'dy', 'd', 'points',
  'width', 'height', 'viewBox', 'preserveAspectRatio', 'transform', 'transform-origin',
  'transform-box', 'pathLength',
  // Painting
  'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-linecap',
  'stroke-linejoin', 'stroke-miterlimit', 'stroke-dasharray', 'stroke-dashoffset',
  'stroke-opacity', 'opacity', 'color', 'display', 'visibility', 'overflow', 'clip',
  'clip-path', 'clip-rule', 'mask', 'filter', 'marker', 'marker-start', 'marker-mid',
  'marker-end', 'paint-order', 'vector-effect', 'shape-rendering', 'text-rendering',
  'image-rendering', 'color-rendering', 'color-interpolation',
  'color-interpolation-filters', 'mix-blend-mode', 'isolation', 'enable-background',
  'stop-color', 'stop-opacity', 'flood-color', 'flood-opacity', 'lighting-color',
  // Text
  'font', 'font-family', 'font-size', 'font-size-adjust', 'font-style', 'font-weight',
  'font-variant', 'font-stretch', 'text-anchor', 'text-decoration', 'letter-spacing',
  'word-spacing', 'white-space', 'kerning', 'dominant-baseline', 'alignment-baseline',
  'baseline-shift', 'writing-mode', 'direction', 'unicode-bidi', 'rotate', 'textLength',
  'lengthAdjust', 'startOffset', 'method', 'spacing', 'side',
  // Gradients and patterns
  'gradientUnits', 'gradientTransform', 'spreadMethod', 'offset', 'fx', 'fy', 'fr',
  'patternUnits', 'patternContentUnits', 'patternTransform',
  // Clipping, masking, markers
  'clipPathUnits', 'maskUnits', 'maskContentUnits', 'mask-type', 'markerUnits',
  'markerWidth', 'markerHeight', 'refX', 'refY', 'orient',
  // Filter primitives
  'filterUnits', 'primitiveUnits', 'in', 'in2', 'result', 'stdDeviation', 'mode', 'type',
  'values', 'tableValues', 'slope', 'intercept', 'amplitude', 'exponent', 'operator',
  'k1', 'k2', 'k3', 'k4', 'order', 'kernelMatrix', 'divisor', 'bias', 'targetX',
  'targetY', 'edgeMode', 'kernelUnitLength', 'preserveAlpha', 'surfaceScale',
  'diffuseConstant', 'specularConstant', 'specularExponent', 'limitingConeAngle',
  'pointsAtX', 'pointsAtY', 'pointsAtZ', 'azimuth', 'elevation', 'z', 'scale',
  'xChannelSelector', 'yChannelSelector', 'radius', 'baseFrequency', 'numOctaves',
  'seed', 'stitchTiles',
  // switch, style
  'systemLanguage', 'media',
]);

/** Embedded image types accepted in `image` and `feImage` references and in CSS `url()`. */
const RASTER_DATA_PREFIXES: readonly string[] = [
  'data:image/png;base64,',
  'data:image/jpeg;base64,',
  'data:image/jpg;base64,',
  'data:image/gif;base64,',
  'data:image/webp;base64,',
];

/** Embedded font types accepted, with the image types, in CSS `url()`. */
const FONT_DATA_PREFIXES: readonly string[] = [
  'data:font/woff;base64,',
  'data:font/woff2;base64,',
  'data:font/ttf;base64,',
  'data:font/otf;base64,',
  'data:application/font-woff;base64,',
  'data:application/font-woff2;base64,',
  'data:application/x-font-woff;base64,',
  'data:application/x-font-ttf;base64,',
];
const CSS_DATA_PREFIXES: readonly string[] = [...RASTER_DATA_PREFIXES, ...FONT_DATA_PREFIXES];
const LONGEST_DATA_PREFIX = Math.max(...CSS_DATA_PREFIXES.map((prefix) => prefix.length));

/**
 * After `cleanCss` has done its work none of these can be left; if one is,
 * the cleaner has a bug and the whole style text is dropped. The function
 * names are the ones that take an address as a plain string, with no
 * `url()` around it.
 */
const CSS_REFUSED_MARKERS: readonly string[] = [
  '@import', 'expression(', 'image-set(', 'image(', 'src(', 'javascript:', '-moz-binding', 'behavior:',
];

const CODE_TAB = 0x09;
const CODE_LF = 0x0a;
const CODE_CR = 0x0d;
const CODE_SPACE = 0x20;
const CODE_BANG = 0x21;
const CODE_QUOTE = 0x22;
const CODE_HASH = 0x23;
const CODE_APOSTROPHE = 0x27;
const CODE_STAR = 0x2a;
const CODE_SLASH = 0x2f;
const CODE_COLON = 0x3a;
const CODE_SEMICOLON = 0x3b;
const CODE_LT = 0x3c;
const CODE_EQUALS = 0x3d;
const CODE_GT = 0x3e;
const CODE_QUESTION = 0x3f;
const CODE_AT = 0x40;
const CODE_LEFT_BRACKET = 0x5b;
const CODE_RIGHT_BRACKET = 0x5d;
const CODE_LOWER_U = 0x75;
const CODE_LEFT_BRACE = 0x7b;
const CODE_RIGHT_BRACE = 0x7d;

// Every pattern below is one character class, alone or with bounded
// repetition: constant work per position of the text it is applied to.
const UPPERCASE_ASCII = /[A-Z]/;
const UPPERCASE_ASCII_ALL = /[A-Z]/g;
const NOT_XML_SPACE = /[^ \t\n\r]/;
const XML_FORBIDDEN_CHAR = /[\u0000-\u0008\u000b\u000c\u000e-\u001f￾￿]/;
const ATTRIBUTE_LINE_SPACE = /[\t\n\r]/g;
const TEXT_ESCAPES = /[&<>]/g;
const ATTRIBUTE_ESCAPES = /[&<>"\t\n\r]/g;
const CSS_TRIGGER = /[\\(@/]/;
const ANY_SPACE_ALL = /[ \t\n\r\f]/g;
/** The five built-in entities and numeric character references of bounded length. */
const CHARACTER_REFERENCE = /&(#x[0-9a-fA-F]{1,6}|#[0-9]{1,7}|lt|gt|amp|quot|apos);/g;

const BUILT_IN_ENTITIES: Readonly<Record<string, string>> = {
  lt: '<',
  gt: '>',
  amp: '&',
  quot: '"',
  apos: "'",
};

const ESCAPED: Readonly<Record<string, string>> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  '\t': '&#9;',
  '\n': '&#10;',
  '\r': '&#13;',
};

function isXmlSpace(code: number): boolean {
  return code === CODE_SPACE || code === CODE_TAB || code === CODE_LF || code === CODE_CR;
}

/** Where a tag or attribute name stops: space, "/", ">", "=", a quote, "<", or the end. */
function isNameEnd(code: number): boolean {
  return (
    isXmlSpace(code) ||
    code === CODE_SLASH ||
    code === CODE_GT ||
    code === CODE_EQUALS ||
    code === CODE_QUOTE ||
    code === CODE_APOSTROPHE ||
    code === CODE_LT
  );
}

/** True for a code point XML 1.0 allows in a document. */
function isXmlChar(codePoint: number): boolean {
  return (
    codePoint === CODE_TAB ||
    codePoint === CODE_LF ||
    codePoint === CODE_CR ||
    (codePoint >= 0x20 && codePoint <= 0xd7ff) ||
    (codePoint >= 0xe000 && codePoint <= 0xfffd) ||
    (codePoint >= 0x10000 && codePoint <= 0x10ffff)
  );
}

/**
 * Lower-case form for keyword matching, always of the same length as the
 * input so that indexes carry over. The built-in conversion is used when it
 * keeps the length (it can lengthen a few non-ASCII letters); otherwise only
 * A to Z are converted.
 */
function asciiLowerCase(text: string): string {
  if (!UPPERCASE_ASCII.test(text)) return text;
  const lowered = text.toLowerCase();
  if (lowered.length === text.length) return lowered;
  return text.replace(UPPERCASE_ASCII_ALL, (letter) => letter.toLowerCase());
}

/**
 * Replaces the references XML defines without a document type: `&lt;`,
 * `&gt;`, `&amp;`, `&quot;`, `&apos;` and numeric ones. A numeric reference
 * to a character XML forbids becomes nothing. Any other "&..." is left as
 * the text it is (its entity was defined, if at all, in the document type
 * declaration, which is not read).
 */
function decodeReferences(raw: string): string {
  if (raw.indexOf('&') === -1) return raw;
  return raw.replace(CHARACTER_REFERENCE, (_whole, body: string) => {
    const builtIn = BUILT_IN_ENTITIES[body];
    if (builtIn !== undefined) return builtIn;
    const codePoint = body.charAt(1) === 'x' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
    return isXmlChar(codePoint) ? String.fromCodePoint(codePoint) : '';
  });
}

/** An attribute value as an XML reader reports it: line breaks and tabs become spaces, then references. */
function decodeAttributeValue(raw: string): string {
  return decodeReferences(raw.replace(ATTRIBUTE_LINE_SPACE, ' '));
}

function escapeText(text: string): string {
  return text.replace(TEXT_ESCAPES, (char) => ESCAPED[char]);
}

function escapeAttributeValue(value: string): string {
  return value.replace(ATTRIBUTE_ESCAPES, (char) => ESCAPED[char]);
}

/** True when every character is in the base64 alphabet. One pass. */
function isBase64Text(text: string): boolean {
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    const ok =
      (code >= 0x30 && code <= 0x39) || // 0-9
      (code >= 0x41 && code <= 0x5a) || // A-Z
      (code >= 0x61 && code <= 0x7a) || // a-z
      code === 0x2b || // +
      code === CODE_SLASH ||
      code === CODE_EQUALS;
    if (!ok) return false;
  }
  return true;
}

/** True for `data:<one of the listed types>;base64,<base64 text and nothing else>`. */
function isEmbeddedData(value: string, prefixes: readonly string[]): boolean {
  const head = asciiLowerCase(value.slice(0, LONGEST_DATA_PREFIX));
  for (const prefix of prefixes) {
    if (head.startsWith(prefix)) return isBase64Text(value.slice(prefix.length));
  }
  return false;
}

/**
 * The value to store for `href` or `xlink:href` on `element`, or null when
 * the reference is not allowed there. A URL reader drops tabs and line breaks
 * anywhere and leading spaces and control characters, so they are dropped
 * before the test, and the stored value is the tested one.
 */
function cleanReference(element: string, value: string): string | null {
  let start = 0;
  while (start < value.length && value.charCodeAt(start) <= CODE_SPACE) start += 1;
  const address = value.slice(start).replace(ATTRIBUTE_LINE_SPACE, '');
  if (address.charCodeAt(0) === CODE_HASH) return address;
  if (element === 'a') {
    const scheme = asciiLowerCase(address.slice(0, 8));
    const allowed = scheme.startsWith('http://') || scheme.startsWith('https://') || scheme.startsWith('mailto:');
    return allowed ? address : null;
  }
  if (element === 'image' || element === 'feImage') {
    const compact = address.replace(ANY_SPACE_ALL, '');
    return isEmbeddedData(compact, RASTER_DATA_PREFIXES) ? compact : null;
  }
  return null;
}

function isCssSpace(code: number): boolean {
  return isXmlSpace(code) || code === 0x0c;
}

/** `text` without leading and trailing CSS white space. */
function trimCssSpace(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && isCssSpace(text.charCodeAt(start))) start += 1;
  while (end > start && isCssSpace(text.charCodeAt(end - 1))) end -= 1;
  return text.slice(start, end);
}

/**
 * True when the argument of a CSS `url(...)` is `#id` or embedded image or
 * font data. The argument is left as written when it is kept, so it is
 * judged exactly: white space is allowed only around it and just inside its
 * quotes.
 */
function isAllowedCssUrl(argument: string): boolean {
  let value = trimCssSpace(argument);
  const quote = value.charAt(0);
  if (quote === '"' || quote === "'") {
    value = value.slice(1);
    if (value.endsWith(quote)) value = value.slice(0, -1);
    value = trimCssSpace(value);
  }
  if (value.charCodeAt(0) === CODE_HASH) return true;
  return isEmbeddedData(value, CSS_DATA_PREFIXES);
}

/**
 * CSS (a style sheet, a `style` attribute, or the value of a presentation
 * attribute) with everything that can load another file taken out:
 *
 *  - a comment becomes one space (a space, not nothing: a comment separates
 *    two tokens, and joining them could spell a new one);
 *  - `@import` up to the next ";" becomes one space;
 *  - `url(...)` stays when its argument is `#id` or embedded image or font
 *    data and becomes `none` otherwise.
 *
 * Text that contains a backslash is refused whole (the result is empty): a
 * CSS escape can spell any of the words above, and a logo's styles have no
 * use for one. The scan does not track strings: an `@import` or `url(`
 * inside a quoted string is treated like one outside, which can only remove
 * more. An unclosed comment, rule or `url(` takes the rest of the text with
 * it. One forward pass.
 */
export function cleanCss(css: string): string {
  if (!CSS_TRIGGER.test(css)) return css;
  if (css.indexOf('\\') !== -1) return '';
  const lower = asciiLowerCase(css);
  const length = css.length;
  const kept: string[] = [];
  let copiedTo = 0;
  let pos = 0;
  while (pos < length) {
    const code = lower.charCodeAt(pos);
    let end = -1;
    let replacement = ' ';
    if (code === CODE_SLASH && lower.charCodeAt(pos + 1) === CODE_STAR) {
      const close = lower.indexOf('*/', pos + 2);
      end = close === -1 ? length : close + 2;
    } else if (code === CODE_AT && lower.startsWith('@import', pos)) {
      const close = lower.indexOf(';', pos);
      end = close === -1 ? length : close + 1;
    } else if (code === CODE_LOWER_U && lower.startsWith('url(', pos)) {
      const close = lower.indexOf(')', pos + 4);
      end = close === -1 ? length : close + 1;
      if (close !== -1 && isAllowedCssUrl(css.slice(pos + 4, close))) {
        pos = end;
        continue;
      }
      replacement = 'none';
    }
    if (end === -1) {
      pos += 1;
      continue;
    }
    kept.push(css.slice(copiedTo, pos), replacement);
    copiedTo = end;
    pos = end;
  }
  kept.push(css.slice(copiedTo));
  const cleaned = kept.join('');
  const check = asciiLowerCase(cleaned);
  for (const marker of CSS_REFUSED_MARKERS) {
    if (check.includes(marker)) return '';
  }
  return cleaned;
}

/**
 * The value to store for attribute `name` on `element`, or null when the
 * attribute is left out. `raw` is the text between the quotes.
 */
function cleanAttribute(element: string, name: string, raw: string): string | null {
  if (!ALLOWED_ATTRIBUTES.has(name)) return null;
  const value = decodeAttributeValue(raw);
  if (name === 'href' || name === 'xlink:href') return cleanReference(element, value);
  const css = cleanCss(value);
  if (name === 'style') return NOT_XML_SPACE.test(css) ? css : null;
  // Presentation attributes are read as CSS too. One the cleaner would have
  // to change is not worth keeping in part.
  return css === value ? value : null;
}

/** A color the document names and how many times it does. */
export interface SvgColorCount {
  red: number;
  green: number;
  blue: number;
  count: number;
}

/** What `cleanSvg` returns for a document it accepts. */
export interface CleanedSvg {
  /** The cleaned document. */
  svg: string;
  /** Size in CSS pixels, from the root's `width`, `height` and `viewBox`. */
  width: number;
  height: number;
  /** The colors named in the kept parts, in order of first appearance. */
  colors: SvgColorCount[];
}

/** Properties (and presentation attributes) whose value is a color. */
const COLOR_PROPERTIES: ReadonlySet<string> = new Set([
  'fill', 'stroke', 'stop-color', 'flood-color', 'lighting-color', 'color',
]);

/** Distinct colors counted per document; later new ones are ignored. Bounds what the worker sends back. */
const MAX_DISTINCT_COLORS = 4096;

/** A color value longer than this is not a hex color, an `rgb()` or a name, and is not parsed. */
const MAX_COLOR_TEXT = 64;

/** The CSS 2.1 color names and the extended names most often seen in exported logos. */
const NAMED_COLORS: Readonly<Record<string, number>> = {
  black: 0x000000, white: 0xffffff, red: 0xff0000, lime: 0x00ff00, blue: 0x0000ff,
  yellow: 0xffff00, aqua: 0x00ffff, cyan: 0x00ffff, fuchsia: 0xff00ff, magenta: 0xff00ff,
  gray: 0x808080, grey: 0x808080, silver: 0xc0c0c0, maroon: 0x800000, olive: 0x808000,
  green: 0x008000, purple: 0x800080, teal: 0x008080, navy: 0x000080, orange: 0xffa500,
  pink: 0xffc0cb, brown: 0xa52a2a, gold: 0xffd700, indigo: 0x4b0082, violet: 0xee82ee,
  crimson: 0xdc143c, coral: 0xff7f50, salmon: 0xfa8072, tomato: 0xff6347, turquoise: 0x40e0d0,
  beige: 0xf5f5dc, ivory: 0xfffff0, khaki: 0xf0e68c, tan: 0xd2b48c, skyblue: 0x87ceeb,
  steelblue: 0x4682b4, royalblue: 0x4169e1, darkblue: 0x00008b, darkgreen: 0x006400,
  darkred: 0x8b0000, darkgray: 0xa9a9a9, darkgrey: 0xa9a9a9, lightgray: 0xd3d3d3,
  lightgrey: 0xd3d3d3,
};

// Applied only to text of at most MAX_COLOR_TEXT characters, and every
// repetition in them is bounded.
const HEX_COLOR = /^#([0-9a-f]{3,8})$/;
const RGB_COLOR = /^rgba?\(([0-9.]{1,10}%?)[ ,]{1,4}([0-9.]{1,10}%?)[ ,]{1,4}([0-9.]{1,10}%?)[ ,/0-9.%]{0,16}\)$/;
const IMPORTANT_SUFFIX = /[ ]{0,8}![ ]{0,8}important$/;

function rgbChannel(text: string): number {
  const amount = parseFloat(text);
  if (!Number.isFinite(amount)) return 0;
  const scaled = text.endsWith('%') ? (amount * 255) / 100 : amount;
  return Math.min(255, Math.max(0, Math.round(scaled)));
}

function hexColor(digits: string): number | null {
  if (digits.length === 3 || digits.length === 4) {
    const red = parseInt(digits.charAt(0), 16) * 17;
    const green = parseInt(digits.charAt(1), 16) * 17;
    const blue = parseInt(digits.charAt(2), 16) * 17;
    return (red << 16) | (green << 8) | blue;
  }
  if (digits.length === 6 || digits.length === 8) return parseInt(digits.slice(0, 6), 16);
  return null;
}

/**
 * The color `raw` names as 0xRRGGBB, or null when it is not a hex color, an
 * `rgb()`/`rgba()` or a name in `NAMED_COLORS`. Alpha is ignored.
 */
export function parseColor(raw: string): number | null {
  if (raw.length > MAX_COLOR_TEXT) return null;
  const text = asciiLowerCase(trimCssSpace(raw)).replace(IMPORTANT_SUFFIX, '');
  const hex = HEX_COLOR.exec(text);
  if (hex) return hexColor(hex[1]);
  const rgb = RGB_COLOR.exec(text);
  if (rgb) return (rgbChannel(rgb[1]) << 16) | (rgbChannel(rgb[2]) << 8) | rgbChannel(rgb[3]);
  const named = Object.prototype.hasOwnProperty.call(NAMED_COLORS, text) ? NAMED_COLORS[text] : undefined;
  return named === undefined ? null : named;
}

/** Largest width or height reported, in CSS pixels. */
const MAX_SVG_DIMENSION = 16384;
/** The size a browser gives an SVG that states none: the CSS default for a replaced element. */
const DEFAULT_SVG_WIDTH = 300;
const DEFAULT_SVG_HEIGHT = 150;

// Applied only to text of bounded length (32 and 128 characters).
const ABSOLUTE_LENGTH = /^([0-9]{1,7}(?:\.[0-9]{1,6})?|\.[0-9]{1,6})(px|pt|pc|mm|cm|in)?$/;
const VIEWBOX_SEPARATOR = /[ ,]{1,16}/;

const PIXELS_PER_UNIT: Readonly<Record<string, number>> = {
  px: 1,
  pt: 96 / 72,
  pc: 16,
  mm: 96 / 25.4,
  cm: 96 / 2.54,
  in: 96,
};

/** A `width` or `height` in CSS pixels, or null when it is absent, relative (`%`, `em`) or not positive. */
function absoluteLength(raw: string | null): number | null {
  if (raw === null || raw.length > 32) return null;
  const match = ABSOLUTE_LENGTH.exec(asciiLowerCase(trimCssSpace(raw)));
  if (!match) return null;
  const pixels = parseFloat(match[1]) * PIXELS_PER_UNIT[match[2] ?? 'px'];
  return pixels > 0 ? pixels : null;
}

/** The width and height of a `viewBox`, or null when it does not hold four numbers with a positive size. */
function viewBoxSize(raw: string | null): { width: number; height: number } | null {
  if (raw === null || raw.length > 128) return null;
  const parts = trimCssSpace(raw).split(VIEWBOX_SEPARATOR);
  if (parts.length !== 4) return null;
  const width = Number(parts[2]);
  const height = Number(parts[3]);
  const usable = Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0;
  return usable ? { width, height } : null;
}

function clampDimension(pixels: number): number {
  return Math.min(MAX_SVG_DIMENSION, Math.max(1, Math.round(pixels)));
}

/**
 * The size of the drawing in whole CSS pixels, the way a browser sizes an
 * `<img>` of it: an absolute `width` and `height` are used as they are; one
 * that is missing or relative comes from the `viewBox` (keeping its
 * proportions when the other is given); with neither, 300 by 150. Each side
 * is kept between 1 and `MAX_SVG_DIMENSION`.
 */
export function rootSize(
  widthText: string | null,
  heightText: string | null,
  viewBoxText: string | null
): { width: number; height: number } {
  let width = absoluteLength(widthText);
  let height = absoluteLength(heightText);
  const box = viewBoxSize(viewBoxText);
  if (box && width === null && height === null) {
    width = box.width;
    height = box.height;
  } else if (box && width === null && height !== null) {
    width = (height * box.width) / box.height;
  } else if (box && height === null && width !== null) {
    height = (width * box.height) / box.width;
  }
  return {
    width: clampDimension(width ?? DEFAULT_SVG_WIDTH),
    height: clampDimension(height ?? DEFAULT_SVG_HEIGHT),
  };
}

/** The forward-only reader and writer behind `cleanSvg`. One instance per document. */
class SvgRewriter {
  private pos = 0;
  private readonly out: string[] = [XML_DECLARATION];
  private outLength = XML_DECLARATION.length;
  /** Names of the elements that are open, kept or not. */
  private readonly openNames: string[] = [];
  /** Index in `openNames` of the outermost element being left out, or -1. */
  private dropFrom = -1;
  /** Text of the `style` element being read, or null outside one. */
  private styleText: string[] | null = null;
  private sawRoot = false;
  private rootClosed = false;
  private readonly seenAttributes = new Set<string>();
  /** The root's `width`, `height` and `viewBox` as written (decoded), or null. */
  private rootWidth: string | null = null;
  private rootHeight: string | null = null;
  private rootViewBox: string | null = null;
  /** 0xRRGGBB to the number of times the kept parts name it. */
  private readonly colorCounts = new Map<number, number>();

  constructor(private readonly src: string) {}

  /** The root's size, read by `run`. */
  size(): { width: number; height: number } {
    return rootSize(this.rootWidth, this.rootHeight, this.rootViewBox);
  }

  /** The colors counted by `run`, in order of first appearance. */
  colors(): SvgColorCount[] {
    const counted: SvgColorCount[] = [];
    for (const [rgb, count] of this.colorCounts) {
      counted.push({ red: (rgb >> 16) & 0xff, green: (rgb >> 8) & 0xff, blue: rgb & 0xff, count });
    }
    return counted;
  }

  private countColor(value: string): void {
    const rgb = parseColor(value);
    if (rgb === null) return;
    const seen = this.colorCounts.get(rgb);
    if (seen !== undefined) this.colorCounts.set(rgb, seen + 1);
    else if (this.colorCounts.size < MAX_DISTINCT_COLORS) this.colorCounts.set(rgb, 1);
  }

  /**
   * Counts the colors in the declarations of cleaned CSS (`property: value`
   * between ";", "{" and "}"). One pass; a piece with no ":" costs nothing
   * more than reading it.
   */
  private countCssColors(css: string): void {
    let start = 0;
    let colonAt = -1;
    for (let index = 0; index <= css.length; index += 1) {
      const code = index < css.length ? css.charCodeAt(index) : CODE_SEMICOLON;
      if (code === CODE_COLON && colonAt === -1) colonAt = index;
      if (code !== CODE_SEMICOLON && code !== CODE_LEFT_BRACE && code !== CODE_RIGHT_BRACE) continue;
      // A property name is short; a longer piece before the ":" is a selector.
      if (colonAt !== -1 && colonAt - start <= MAX_COLOR_TEXT) {
        const property = asciiLowerCase(trimCssSpace(css.slice(start, colonAt)));
        if (COLOR_PROPERTIES.has(property)) this.countColor(css.slice(colonAt + 1, index));
      }
      start = index + 1;
      colonAt = -1;
    }
  }

  /** What the kept attribute `name` tells about size (on the root) and color. */
  private noteAttribute(name: string, value: string, onRoot: boolean): void {
    if (name === 'style') this.countCssColors(value);
    else if (COLOR_PROPERTIES.has(name)) this.countColor(value);
    if (!onRoot) return;
    if (name === 'width') this.rootWidth = value;
    else if (name === 'height') this.rootHeight = value;
    else if (name === 'viewBox') this.rootViewBox = value;
  }

  /** The cleaned document, or '' when the source is not a well-formed SVG document. */
  run(): string {
    const longestOutput = longestOutputFor(this.src.length);
    if (this.src.charCodeAt(0) === 0xfeff) this.pos = 1;
    while (!this.rootClosed) {
      const next = this.src.indexOf('<', this.pos);
      if (next === -1) return '';
      if (next > this.pos && !this.text(this.src.slice(this.pos, next), true)) return '';
      this.pos = next;
      if (!this.markup() || this.outLength > longestOutput) return '';
    }
    return this.out.join('');
  }

  private write(piece: string): void {
    this.out.push(piece);
    this.outLength += piece.length;
  }

  /** Character data. `encoded` is false for the content of a CDATA section, which has no references. */
  private text(raw: string, encoded: boolean): boolean {
    if (this.openNames.length === 0) return encoded && !NOT_XML_SPACE.test(raw);
    if (this.dropFrom !== -1) return true;
    const value = encoded ? decodeReferences(raw) : raw;
    if (this.styleText) this.styleText.push(value);
    else this.write(escapeText(value));
    return true;
  }

  /** Reads the token that starts with the "<" at `pos`. False when it is not well-formed. */
  private markup(): boolean {
    const code = this.src.charCodeAt(this.pos + 1);
    if (code === CODE_BANG) {
      if (this.src.startsWith('<!--', this.pos)) return this.skipPast('-->', 4);
      if (this.src.startsWith('<![CDATA[', this.pos)) return this.cdata();
      return this.declaration();
    }
    if (code === CODE_QUESTION) return this.skipPast('?>', 2);
    if (code === CODE_SLASH) return this.endTag();
    return this.startTag();
  }

  /** Moves past the next `terminator`; false when there is none. Used for comments and processing instructions. */
  private skipPast(terminator: string, openerLength: number): boolean {
    const end = this.src.indexOf(terminator, this.pos + openerLength);
    if (end === -1) return false;
    this.pos = end + terminator.length;
    return true;
  }

  private cdata(): boolean {
    const end = this.src.indexOf(']]>', this.pos + 9);
    if (end === -1) return false;
    const content = this.src.slice(this.pos + 9, end);
    this.pos = end + 3;
    return this.text(content, false);
  }

  /**
   * Moves past a `<!DOCTYPE ...>` declaration, internal subset included,
   * copying nothing. Its end is the first ">" that is outside quotes,
   * comments, processing instructions and the "[...]" subset. Only legal
   * before the root element.
   */
  private declaration(): boolean {
    if (this.sawRoot) return false;
    const src = this.src;
    let depth = 0;
    let cursor = this.pos + 2;
    while (cursor < src.length) {
      const code = src.charCodeAt(cursor);
      let jumpTo = cursor + 1;
      if (code === CODE_QUOTE || code === CODE_APOSTROPHE) {
        jumpTo = src.indexOf(src.charAt(cursor), cursor + 1) + 1;
      } else if (code === CODE_LT && src.startsWith('<!--', cursor)) {
        const end = src.indexOf('-->', cursor + 4);
        jumpTo = end === -1 ? 0 : end + 3;
      } else if (code === CODE_LT && src.charCodeAt(cursor + 1) === CODE_QUESTION) {
        const end = src.indexOf('?>', cursor + 2);
        jumpTo = end === -1 ? 0 : end + 2;
      } else if (code === CODE_LEFT_BRACKET) {
        depth += 1;
      } else if (code === CODE_RIGHT_BRACKET) {
        depth -= 1;
      } else if (code === CODE_GT && depth <= 0) {
        this.pos = cursor + 1;
        return true;
      }
      if (jumpTo === 0) return false; // an unclosed quote, comment or instruction
      cursor = jumpTo;
    }
    return false;
  }

  private skipSpace(from: number): number {
    let cursor = from;
    while (cursor < this.src.length && isXmlSpace(this.src.charCodeAt(cursor))) cursor += 1;
    return cursor;
  }

  private nameEnd(from: number): number {
    let cursor = from;
    while (cursor < this.src.length && !isNameEnd(this.src.charCodeAt(cursor))) cursor += 1;
    return cursor;
  }

  private startTag(): boolean {
    const nameStop = this.nameEnd(this.pos + 1);
    if (nameStop === this.pos + 1) return false;
    const name = this.src.slice(this.pos + 1, nameStop);
    const isRoot = !this.sawRoot;
    if (isRoot && name !== 'svg') return false;
    this.sawRoot = true;
    const kept = this.dropFrom === -1 && this.styleText === null && ALLOWED_ELEMENTS.has(name);
    if (kept) this.write(isRoot ? `<${name}${ROOT_NAMESPACES}` : `<${name}`);
    const selfClosing = this.attributes(nameStop, kept ? name : null);
    if (selfClosing === null) return false;
    if (kept) this.write(selfClosing ? '/>' : '>');
    if (selfClosing) {
      if (isRoot) this.rootClosed = true;
      return true;
    }
    if (this.openNames.length >= MAX_ELEMENT_DEPTH) return false;
    this.openNames.push(name);
    if (!kept && this.dropFrom === -1) this.dropFrom = this.openNames.length - 1;
    if (kept && name === 'style') this.styleText = [];
    return true;
  }

  /**
   * Reads the attributes of the start tag whose name ended at `from`, up to
   * and including its ">" or "/>". When `element` is given (the tag is being
   * kept) the allowed ones are written. Returns whether the tag closed
   * itself, or null when it is not well-formed: no space before an
   * attribute, no "=", a value without quotes or with "<" in it, the same
   * name twice.
   */
  private attributes(from: number, element: string | null): boolean | null {
    const src = this.src;
    this.seenAttributes.clear();
    let cursor = from;
    for (;;) {
      const afterSpace = this.skipSpace(cursor);
      const code = src.charCodeAt(afterSpace);
      if (code === CODE_GT) {
        this.pos = afterSpace + 1;
        return false;
      }
      if (code === CODE_SLASH && src.charCodeAt(afterSpace + 1) === CODE_GT) {
        this.pos = afterSpace + 2;
        return true;
      }
      if (afterSpace === cursor) return null;
      const nameStop = this.nameEnd(afterSpace);
      const equalsAt = this.skipSpace(nameStop);
      if (nameStop === afterSpace || src.charCodeAt(equalsAt) !== CODE_EQUALS) return null;
      const quoteAt = this.skipSpace(equalsAt + 1);
      const quote = src.charCodeAt(quoteAt);
      if (quote !== CODE_QUOTE && quote !== CODE_APOSTROPHE) return null;
      const closeAt = src.indexOf(src.charAt(quoteAt), quoteAt + 1);
      if (closeAt === -1) return null;
      const name = src.slice(afterSpace, nameStop);
      const raw = src.slice(quoteAt + 1, closeAt);
      if (this.seenAttributes.has(name) || raw.indexOf('<') !== -1) return null;
      this.seenAttributes.add(name);
      if (element !== null) {
        const value = cleanAttribute(element, name, raw);
        if (value !== null) {
          this.write(` ${name}="${escapeAttributeValue(value)}"`);
          // The root is the one start tag read while no element is open.
          this.noteAttribute(name, value, this.openNames.length === 0);
        }
      }
      cursor = closeAt + 1;
    }
  }

  private endTag(): boolean {
    const close = this.src.indexOf('>', this.pos + 2);
    if (close === -1) return false;
    let nameStop = close;
    while (nameStop > this.pos + 2 && isXmlSpace(this.src.charCodeAt(nameStop - 1))) nameStop -= 1;
    const name = this.src.slice(this.pos + 2, nameStop);
    const top = this.openNames.length - 1;
    if (top < 0 || this.openNames[top] !== name) return false;
    this.openNames.pop();
    this.pos = close + 1;
    if (this.dropFrom === top) {
      this.dropFrom = -1;
    } else if (this.dropFrom === -1) {
      if (this.styleText) {
        const css = cleanCss(this.styleText.join(''));
        this.countCssColors(css);
        this.write(escapeText(css));
        this.styleText = null;
      }
      this.write(`</${name}>`);
    }
    if (top === 0) this.rootClosed = true;
    return true;
  }
}

/**
 * A new SVG document made from the listed parts of `svg` (see the top of
 * this file), with the size and colors read from it on the way, or null when
 * `svg` is longer than `MAX_SVG_LENGTH`, contains a character XML forbids, is
 * not well-formed XML with an `svg` root, or would come out more than an
 * eighth (plus 1024 characters) longer than it went in. Takes time
 * proportional to the length of `svg`.
 */
export function cleanSvg(svg: string): CleanedSvg | null {
  if (svg.length > MAX_SVG_LENGTH || XML_FORBIDDEN_CHAR.test(svg)) return null;
  const rewriter = new SvgRewriter(svg);
  const cleaned = rewriter.run();
  if (!cleaned) return null;
  return { svg: cleaned, ...rewriter.size(), colors: rewriter.colors() };
}

/** The document `cleanSvg` writes, or the empty string when it refuses `svg`. */
export function sanitizeSvg(svg: string): string {
  const cleaned = cleanSvg(svg);
  return cleaned ? cleaned.svg : '';
}
