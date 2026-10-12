import { describe, it, expect } from 'vitest';
import sharp from 'sharp';
import { processLogo } from '../../src/services/logo';
import { cleanSvg, parseColor, rootSize } from '../../src/services/svg_sanitize';

/**
 * An uploaded SVG is not drawn on the server, so the two things the upload
 * route reports about it come from its text: the size, from the root's
 * `width`, `height` and `viewBox`, and the palette, from how often each color
 * is named. This file covers both, and sets the palette next to the one the
 * earlier code got by drawing the SVG and counting pixels (reproduced here as
 * `drawnPalette`, on ordinary logos only: drawing is safe for those).
 *
 * The two palettes are not the same thing and do not always agree. Where they
 * differ, and why that is acceptable, is spelled out case by case below.
 *
 * Who reads the result: the intake route stores the three colors in
 * `spec.design` and returns them with the size; the intake page paints three
 * swatches and does not read the size; the v2 engine reads only
 * `design.primary_color`, as a hint toward one of its own palettes; the v1
 * engine reads primary and secondary as `#rrggbb`. All of them need
 * `#rrggbb` strings and nothing else.
 */

const NS = 'xmlns="http://www.w3.org/2000/svg"';

function hex(red: number, green: number, blue: number): string {
  const channel = (value: number): string => Math.max(0, Math.min(255, value)).toString(16).padStart(2, '0');
  return `#${channel(red)}${channel(green)}${channel(blue)}`;
}

/** The colors the earlier code found: draw at 64 x 64, count pixels per bucket, most frequent first. */
async function drawnPalette(svg: string): Promise<string[]> {
  const { data } = await sharp(Buffer.from(svg))
    .resize(64, 64, { fit: 'cover' })
    .removeAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });
  const counts = new Map<string, number>();
  for (let index = 0; index < data.length; index += 3) {
    const red = Math.round(data[index] / 32) * 32;
    const green = Math.round(data[index + 1] / 32) * 32;
    const blue = Math.round(data[index + 2] / 32) * 32;
    const brightness = (red + green + blue) / 3;
    if (brightness > 240 || brightness < 15) continue;
    const spread = Math.max(red, green, blue) - Math.min(red, green, blue);
    if (spread < 20 && brightness > 50 && brightness < 200) continue;
    const key = `${red},${green},${blue}`;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([key]) => {
      const [red, green, blue] = key.split(',').map(Number);
      return hex(red, green, blue);
    });
}

async function textPalette(svg: string): Promise<string[]> {
  const { colors } = await processLogo(Buffer.from(svg), 'image/svg+xml');
  return [colors.primary_color, colors.secondary_color, colors.accent_color];
}

// ============================================================
// Size
// ============================================================

describe('the size of an SVG comes from its root element', () => {
  it.each([
    ['plain numbers are CSS pixels', '40', '30', null, [40, 30]],
    ['px, with spaces and decimals', ' 40.4px ', '29.6px', null, [40, 30]],
    ['absolute units are converted at 96 per inch', '1in', '25.4mm', null, [96, 96]],
    ['points and picas', '72pt', '6pc', null, [96, 96]],
    ['centimetres', '2.54cm', '1.27cm', null, [96, 48]],
    ['a viewBox alone gives the size', null, null, '0 0 120 40', [120, 40]],
    ['a viewBox with commas and an offset', null, null, '-10,-10,200,100', [200, 100]],
    ['a percentage is relative, so the viewBox decides', '100%', '100%', '0 0 64 48', [64, 48]],
    ['one side given: the other keeps the viewBox proportions', '300', null, '0 0 100 50', [300, 150]],
    ['the other side given', null, '50', '0 0 100 50', [100, 50]],
    ['em is relative', '10em', '2em', '0 0 30 10', [30, 10]],
    ['nothing stated: what a browser assumes', null, null, null, [300, 150]],
    ['only a width', '80', null, null, [80, 150]],
    ['a broken viewBox is ignored', null, null, '0 0 abc 10', [300, 150]],
    ['a viewBox with no area is ignored', null, null, '0 0 0 0', [300, 150]],
    ['zero and negative lengths are not sizes', '0', '-5', null, [300, 150]],
    ['a huge size is capped', '999999', '1e9', null, [16384, 150]],
    ['a tiny size is at least one pixel', '0.2', '0.4', null, [1, 1]],
    ['a value that is not a length', 'javascript:1', 'calc(1px + 1px)', null, [300, 150]],
    ['a very long value is not parsed', '1'.repeat(200), '40', null, [300, 40]],
  ])('%s', (_name, width, height, viewBox, expected) => {
    const size = rootSize(width, height, viewBox);
    expect([size.width, size.height]).toEqual(expected);
  });

  it('reads the root element only, not a nested svg or a style', () => {
    const svg =
      `<svg ${NS} viewBox="0 0 200 100" style="width:999px">` +
      `<svg width="7" height="7"><rect width="7" height="7"/></svg></svg>`;
    expect(cleanSvg(svg)).toMatchObject({ width: 200, height: 100 });
  });

  it('decodes character references before reading a size', () => {
    expect(cleanSvg(`<svg ${NS} width="&#52;0" height="4&#x30;"/>`)).toMatchObject({ width: 40, height: 40 });
  });

  it('processLogo reports that size for a stored SVG', async () => {
    const result = await processLogo(Buffer.from(`<svg ${NS} viewBox="0 0 640 480"><rect width="640" height="480"/></svg>`));
    expect([result.width, result.height]).toEqual([640, 480]);
  });
});

// ============================================================
// Colors
// ============================================================

describe('parseColor', () => {
  it.each([
    ['#1d4ed8', 0x1d4ed8],
    ['#FFF', 0xffffff],
    ['#abc', 0xaabbcc],
    ['#abcd', 0xaabbcc],
    ['#11223380', 0x112233],
    ['  #1D4ED8  ', 0x1d4ed8],
    ['rgb(14, 116, 144)', 0x0e7490],
    ['rgb(14 116 144)', 0x0e7490],
    ['RGBA(255,0,0,0.5)', 0xff0000],
    ['rgb(100%, 50%, 0%)', 0xff8000],
    ['rgb(300, -1, 12.6)', null],
    ['rgb(300, 0, 12.6)', 0xff000d],
    ['teal', 0x008080],
    ['Orange', 0xffa500],
    ['red !important', 0xff0000],
  ])('%s', (text, expected) => {
    expect(parseColor(text)).toBe(expected);
  });

  it.each(['none', 'currentColor', 'transparent', 'inherit', 'url(#gradient)', 'hsl(10, 50%, 50%)', '#12', '#12345', '', 'toString', 'constructor', '#' + 'f'.repeat(100)])(
    '%s is not counted',
    (text) => {
      expect(parseColor(text)).toBeNull();
    }
  );
});

describe('the colors an SVG names are counted where they are written', () => {
  it('counts attributes, style attributes and style sheets, in order of first appearance', () => {
    const svg =
      `<svg ${NS} width="10" height="10">` +
      '<style>.a{fill:#e63946;stroke:NAVY} @media print{.b{stop-color:rgb(1,2,3)}} .c:hover{color:red}</style>' +
      '<rect fill="#e63946" stroke="none"/>' +
      '<g style="fill: #e63946; stroke : teal !important; opacity:.5"><circle flood-color="teal" lighting-color="white"/></g>' +
      '<linearGradient><stop stop-color="#000080"/></linearGradient>' +
      '</svg>';
    expect(cleanSvg(svg)?.colors).toEqual([
      { red: 0xe6, green: 0x39, blue: 0x46, count: 3 },
      { red: 0x00, green: 0x00, blue: 0x80, count: 2 },
      { red: 0x01, green: 0x02, blue: 0x03, count: 1 },
      { red: 0xff, green: 0x00, blue: 0x00, count: 1 },
      { red: 0x00, green: 0x80, blue: 0x80, count: 2 },
      { red: 0xff, green: 0xff, blue: 0xff, count: 1 },
    ]);
  });

  it('does not count what the cleaner leaves out', () => {
    const svg =
      `<svg ${NS} width="10" height="10"><script fill="#ff0000">x</script>` +
      '<foreignObject><p style="color:#00ff00">x</p></foreignObject>' +
      '<rect fill="#0000ff" onclick="fill=#ff00ff"/></svg>';
    expect(cleanSvg(svg)?.colors).toEqual([{ red: 0, green: 0, blue: 0xff, count: 1 }]);
  });

  it('keeps the number of distinct colors bounded', () => {
    let rects = '';
    for (let index = 0; index < 6000; index += 1) rects += `<rect fill="#${index.toString(16).padStart(6, '0')}"/>`;
    expect(cleanSvg(`<svg ${NS}>${rects}</svg>`)?.colors).toHaveLength(4096);
  });
});

// ============================================================
// The palette, next to the one a drawing gave
// ============================================================

describe('the palette read from the text, next to the palette a drawing gave', () => {
  describe('a few flat colors, the larger area written first: the two agree', () => {
    it.each([
      [
        'a two-color mark',
        `<svg ${NS} width="120" height="120"><circle cx="60" cy="60" r="56" fill="#1d4ed8"/><path d="M30 60h60v12H30z" fill="#f59e0b"/></svg>`,
        ['#2040e0', '#ffa000'],
      ],
      [
        'style attributes',
        `<svg ${NS} width="64" height="64"><rect style="fill:#ffcc00" width="64" height="64"/><path style="fill:#d7263d" d="M12 12h40v40H12z"/></svg>`,
        ['#ffc000', '#e02040'],
      ],
      [
        'rgb() colors',
        `<svg ${NS} width="100" height="100"><rect width="100" height="100" fill="rgb(14, 116, 144)"/><circle cx="50" cy="50" r="30" fill="rgb(100%, 80%, 0%)"/></svg>`,
        ['#0080a0', '#ffc000'],
      ],
      [
        'strokes only',
        `<svg ${NS} width="100" height="100"><circle cx="50" cy="50" r="40" fill="none" stroke="#059669" stroke-width="10"/><path d="M30 50h40" stroke="#be123c" stroke-width="10"/></svg>`,
        ['#00a060', '#c02040'],
      ],
    ])('%s', async (_name, svg, expected) => {
      expect((await textPalette(svg)).slice(0, 2)).toEqual(expected);
      expect((await drawnPalette(svg)).slice(0, 2)).toEqual(expected);
    });
  });

  it('one color: both give it, and the same derived second and third', async () => {
    const svg = `<svg ${NS} width="30" height="15"><rect width="30" height="15" fill="#9333ea"/></svg>`;
    expect(await drawnPalette(svg)).toEqual(['#a040e0']);
    expect(await textPalette(svg)).toEqual(['#a040e0', '#8c2ccc', '#ffb42c']);
  });

  it('no color named (a black mark): both fall back to the default palette', async () => {
    const svg = `<svg ${NS} viewBox="0 0 24 24"><path d="M12 2l10 20H2z"/></svg>`;
    expect(await drawnPalette(svg)).toEqual([]);
    expect(await textPalette(svg)).toEqual(['#2563eb', '#1e40af', '#f59e0b']);
  });

  it('difference 1: the drawing was cropped to a square, so a wide logo lost its outer colors', async () => {
    const svg =
      `<svg ${NS} width="90" height="30"><rect width="30" height="30" fill="teal"/>` +
      '<rect x="30" width="30" height="30" fill="orange"/><rect x="60" width="30" height="30" fill="navy"/></svg>';
    // The 64 x 64 "cover" crop of a 90 x 30 drawing is its middle third.
    expect(await drawnPalette(svg)).toEqual(['#ffa000']);
    // The text names all three.
    expect(await textPalette(svg)).toEqual(['#008080', '#ffa000', '#000080']);
  });

  it('difference 2: the drawing counted blended edge pixels as colors of their own', async () => {
    const svg =
      `<svg ${NS} width="256" height="256"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">` +
      '<stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#7c3aed"/></linearGradient></defs>' +
      '<rect width="256" height="256" rx="48" fill="url(#g)"/></svg>';
    const drawn = await drawnPalette(svg);
    const fromText = await textPalette(svg);
    // The text gives the two ends of the gradient, as written.
    expect(fromText.slice(0, 2)).toEqual(['#2060e0', '#8040e0']);
    // The drawing's most frequent bucket is a mixture that appears nowhere in the file.
    expect(fromText).not.toContain(drawn[0]);
    expect(drawn).toContain('#8040e0');
  });

  it('difference 3: the text ranks by mentions, the drawing ranked by area', async () => {
    const svg =
      `<svg ${NS} width="200" height="200"><rect width="200" height="200" fill="#0f766e"/>` +
      '<circle cx="20" cy="20" r="4" fill="#f97316"/><circle cx="40" cy="20" r="4" fill="#f97316"/>' +
      '<circle cx="60" cy="20" r="4" fill="#f97316"/><circle cx="80" cy="20" r="4" fill="#f97316"/></svg>';
    // The plate covers the drawing, so the drawing put it first.
    expect((await drawnPalette(svg))[0]).toBe('#008060');
    // The accent is named four times and the plate once, so the text puts
    // the accent first. Both colors are in the palette either way; which of
    // them is called primary differs.
    expect((await textPalette(svg)).slice(0, 2)).toEqual(['#ff8020', '#008060']);
  });

  it('a color in a style rule counts once, however many elements use the rule', async () => {
    const svg =
      `<svg ${NS} viewBox="0 0 200 80"><style>.a{fill:#e63946}.b{fill:#1d3557}</style>` +
      '<rect class="b" width="200" height="80"/><circle class="a" cx="40" cy="40" r="24"/><rect class="a" x="80" y="28" width="100" height="24"/></svg>';
    expect((await textPalette(svg)).slice(0, 2)).toEqual(['#e04040', '#204060']);
    expect((await drawnPalette(svg)).slice(0, 2)).toEqual(['#204060', '#e04040']);
  });

  it('colors inside an embedded raster image are not seen', async () => {
    const png = await sharp({ create: { width: 16, height: 16, channels: 3, background: '#cc2222' } }).png().toBuffer();
    const svg = `<svg ${NS} width="16" height="16"><image width="16" height="16" href="data:image/png;base64,${png.toString('base64')}"/></svg>`;
    expect(await drawnPalette(svg)).toEqual(['#c02020']);
    expect(await textPalette(svg)).toEqual(['#2563eb', '#1e40af', '#f59e0b']);
  });

  it('always answers three #rrggbb strings', async () => {
    const svgs = [
      `<svg ${NS}/>`,
      `<svg ${NS}><rect fill="white"/><rect fill="black"/><rect fill="#808080"/></svg>`,
      `<svg ${NS}><rect fill="#ff0000"/><rect fill="#00ff00"/><rect fill="#0000ff"/><rect fill="#ffff00"/></svg>`,
    ];
    for (const svg of svgs) {
      for (const color of await textPalette(svg)) expect(color).toMatch(/^#[0-9a-f]{6}$/);
    }
  });
});
