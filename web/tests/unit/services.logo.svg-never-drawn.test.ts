import { describe, it, expect, vi, beforeEach } from 'vitest';
import zlib from 'zlib';

// Counts every call into the image library and passes it through unchanged.
const library = vi.hoisted(() => ({ calls: 0 }));
vi.mock('sharp', async (importOriginal) => {
  const original = (await importOriginal()) as { default: (...args: unknown[]) => unknown };
  const real = original.default;
  const counting = (...args: unknown[]): unknown => {
    library.calls += 1;
    return real(...args);
  };
  return { default: Object.assign(counting, real) };
});

import sharp from 'sharp';
import { processLogo, InvalidLogoError } from '../../src/services/logo';

/**
 * Which uploaded bytes reach the image library (sharp: libvips, and through
 * it librsvg, libxml2 and every other decoder it bundles).
 *
 * The rule in logo.ts: only an upload whose first bytes are a PNG, JPEG or
 * WebP signature is handed to it. An SVG, and anything else that starts like
 * an XML document, is rebuilt and measured by svg_sanitize.ts and is never
 * drawn; everything else is refused before any decoder sees it.
 *
 * Before that rule the library was the first thing to read every upload (its
 * own sniff decided the format), so it parsed raw uploaded SVG, including a
 * gzip-compressed one and one with text in front of it, and it drew the
 * cleaned SVG for its colors. Every "never reaches the library" case below
 * fails on that version: the call count is not zero.
 */

const NS = 'xmlns="http://www.w3.org/2000/svg"';
const SVG = `<svg ${NS} width="40" height="40"><rect width="40" height="40" fill="#2255cc"/></svg>`;

async function outcomeOf(upload: Buffer): Promise<{ calls: number; stored?: string; refused?: unknown }> {
  library.calls = 0;
  try {
    const result = await processLogo(upload, 'image/svg+xml');
    return { calls: library.calls, stored: result.mimetype };
  } catch (err) {
    return { calls: library.calls, refused: err };
  }
}

describe('an SVG upload never reaches the image library', () => {
  beforeEach(() => {
    library.calls = 0;
  });

  it('control: the counter sees the library being used for a PNG', async () => {
    const png = await sharp({ create: { width: 8, height: 8, channels: 3, background: '#cc2222' } }).png().toBuffer();
    const outcome = await outcomeOf(png);
    expect(outcome.stored).toBe('image/png');
    expect(outcome.calls).toBeGreaterThan(0);
  });

  it.each([
    ['a plain SVG', Buffer.from(SVG)],
    ['an SVG after an XML declaration and a doctype', Buffer.from(`<?xml version="1.0"?>\n<!DOCTYPE svg>\n${SVG}`)],
    ['an SVG after a byte order mark and blank lines', Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(`\n\n  ${SVG}`)])],
  ])('%s is stored without one call into it', async (_name, upload) => {
    const outcome = await outcomeOf(upload);
    expect(outcome.stored).toBe('image/svg+xml');
    expect(outcome.calls).toBe(0);
  });

  it.each([
    ['a gzip-compressed SVG', zlib.gzipSync(Buffer.from(SVG))],
    ['an SVG with text in front of it', Buffer.from(`logo ${SVG}`)],
    ['an SVG in UTF-16 with a byte order mark', Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(SVG, 'utf16le')])],
    ['an SVG in UTF-16 without one', Buffer.from(SVG, 'utf16le')],
    ['an HTML page', Buffer.from('<html><body><svg></svg></body></html>')],
    ['an XML document that is not an SVG', Buffer.from('<?xml version="1.0"?><note>hello</note>')],
  ])('%s is refused without one call into it', async (_name, upload) => {
    const outcome = await outcomeOf(upload);
    expect(outcome.refused).toBeInstanceOf(InvalidLogoError);
    expect(outcome.calls).toBe(0);
  });
});

describe('only PNG, JPEG and WebP bytes reach the image library', () => {
  it.each([
    ['GIF', () => sharp({ create: { width: 8, height: 8, channels: 3, background: '#000' } }).gif().toBuffer()],
    ['TIFF', () => sharp({ create: { width: 8, height: 8, channels: 3, background: '#000' } }).tiff().toBuffer()],
    ['AVIF', () => sharp({ create: { width: 8, height: 8, channels: 3, background: '#000' } }).avif().toBuffer()],
  ])('%s bytes are refused before any decoder reads them', async (_name, make) => {
    const outcome = await outcomeOf(await make());
    expect(outcome.refused).toBeInstanceOf(InvalidLogoError);
    expect(outcome.calls).toBe(0);
  });

  it('a file that starts as a PNG and is not one is refused as an invalid logo', async () => {
    const fake = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(4096, 7)]);
    expect((await outcomeOf(fake)).refused).toBeInstanceOf(InvalidLogoError);
  });

  it('a PNG cut off half way is refused as an invalid logo, not a server error', async () => {
    const whole = await sharp({
      create: { width: 400, height: 400, channels: 3, background: '#cc2222', noise: { type: 'gaussian', mean: 128, sigma: 30 } },
    })
      .png()
      .toBuffer();
    const outcome = await outcomeOf(whole.subarray(0, Math.floor(whole.length / 2)));
    expect(outcome.refused).toBeInstanceOf(InvalidLogoError);
  });

  it('a PNG whose header claims 60000 x 60000 pixels is refused from the header alone', async () => {
    // 69 bytes on the wire; decoded it would be 10 GB.
    const chunk = (type: string, data: Buffer): Buffer => {
      const body = Buffer.concat([Buffer.from(type, 'latin1'), data]);
      const length = Buffer.alloc(4);
      length.writeUInt32BE(data.length);
      const crc = Buffer.alloc(4);
      crc.writeUInt32BE(zlib.crc32(body) >>> 0);
      return Buffer.concat([length, body, crc]);
    };
    const header = Buffer.alloc(13);
    header.writeUInt32BE(60000, 0);
    header.writeUInt32BE(60000, 4);
    header[8] = 8;
    header[9] = 2;
    const bomb = Buffer.concat([
      Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
      chunk('IHDR', header),
      chunk('IDAT', zlib.deflateSync(Buffer.alloc(64))),
      chunk('IEND', Buffer.alloc(0)),
    ]);
    const started = performance.now();
    const outcome = await outcomeOf(bomb);
    expect(outcome.refused).toBeInstanceOf(InvalidLogoError);
    expect((outcome.refused as Error).message).toMatch(/pixel limit/);
    expect(performance.now() - started).toBeLessThan(1000);
  });

  it('an animated WebP is read as its first frame only', async () => {
    const frame = await sharp({ create: { width: 600, height: 300, channels: 3, background: '#cc2222' } }).png().toBuffer();
    const animated = await sharp(
      Array.from({ length: 12 }, () => frame),
      { join: { animated: true } }
    )
      .webp({ quality: 20, effort: 0 })
      .toBuffer();
    library.calls = 0;
    const result = await processLogo(animated, 'image/webp');
    expect(result.mimetype).toBe('image/png');
    // One frame, scaled to fit 512: not twelve frames stacked.
    expect([result.width, result.height]).toEqual([512, 256]);
  });
});
