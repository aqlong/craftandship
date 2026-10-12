import { describe, it, expect } from 'vitest';
import sharp from 'sharp';
import { processLogo, sanitizeSvg, InvalidLogoError } from '../../src/services/logo';

async function solidPng(width: number, height: number, background: string): Promise<Buffer> {
  return sharp({ create: { width, height, channels: 3, background } }).png().toBuffer();
}

const EVIL_SVG = [
  '<?xml version="1.0"?>',
  '<?xml-stylesheet href="https://attacker.example/x.css"?>',
  '<!DOCTYPE svg [<!ENTITY boom "boom">]>',
  '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="40" height="40" onload="alert(1)">',
  '  <script>alert(document.cookie)</script>',
  '  <script src="https://attacker.example/x.js"/>',
  '  <style>@import url(https://attacker.example/x.css); rect { fill: #cc2222; }</style>',
  '  <foreignObject width="40" height="40"><body xmlns="http://www.w3.org/1999/xhtml">hi</body></foreignObject>',
  '  <a href="javascript:alert(1)"><rect width="40" height="40" fill="#cc2222"/></a>',
  '  <a xlink:href=" JaVa&#x09;script:alert(2)"><circle cx="20" cy="20" r="8" fill="#2255cc" onclick="alert(3)"/></a>',
  '  <a href="&#106;avascript:alert(4)"><rect width="4" height="4"/></a>',
  '  <a href="https://example.com/ok"><rect width="4" height="4"/></a>',
  '</svg>',
].join('\n');

describe('sanitizeSvg', () => {
  const cleaned = sanitizeSvg(EVIL_SVG);

  it('removes script, foreignObject, event handlers, doctype and stylesheet instructions', () => {
    expect(cleaned).not.toMatch(/<script/i);
    expect(cleaned).not.toMatch(/foreignObject/i);
    expect(cleaned).not.toMatch(/\son[a-z]+\s*=/i);
    expect(cleaned).not.toMatch(/<!DOCTYPE|<!ENTITY/i);
    expect(cleaned).not.toMatch(/xml-stylesheet/i);
    expect(cleaned).not.toMatch(/@import/i);
  });

  it('removes javascript: links however they are spelled, and keeps ordinary ones', () => {
    expect(cleaned).not.toMatch(/javascript/i);
    expect(cleaned).not.toMatch(/&#106;/);
    expect(cleaned).toContain('href="https://example.com/ok"');
  });

  it('keeps the drawing itself and writes a normalized xml declaration', () => {
    // The cleaner rebuilds the document and writes its own UTF-8 declaration
    // (it does not echo the upload's), so the exact declaration differs from
    // the old regex cleaner, which passed the original through.
    expect(cleaned).toContain('<?xml version="1.0" encoding="UTF-8"?>');
    expect(cleaned).toContain('<rect width="40" height="40" fill="#cc2222"/>');
    expect(cleaned).toContain('<circle');
  });
});

describe('processLogo', () => {
  it('stores a cleaned SVG as SVG, still renderable, with no active content', async () => {
    const result = await processLogo(Buffer.from(EVIL_SVG, 'utf8'), 'image/svg+xml');
    expect(result.mimetype).toBe('image/svg+xml');
    const stored = Buffer.from(result.cleanedBase64, 'base64').toString('utf8');
    expect(stored).not.toMatch(/<script|onload|javascript/i);
    expect([result.width, result.height]).toEqual([40, 40]);
    // The red rect is named twice (once in the style sheet), the blue circle once.
    expect(result.colors.primary_color).toBe('#c02020');
    expect(result.colors.secondary_color).toBe('#2060c0');
    // processLogo itself no longer opens an SVG with the image library, so
    // the check that the stored file is one a real SVG reader accepts is here.
    const reread = await sharp(Buffer.from(stored, 'utf8')).metadata();
    expect([reread.format, reread.width, reread.height]).toEqual(['svg', 40, 40]);
  });

  it('re-encodes a raster upload to PNG and caps it at 512px', async () => {
    const jpeg = await sharp({ create: { width: 1200, height: 600, channels: 3, background: '#2563eb' } })
      .jpeg()
      .toBuffer();
    const result = await processLogo(jpeg, 'image/jpeg');
    expect(result.mimetype).toBe('image/png');
    expect(result.width).toBe(512);
    expect(result.height).toBe(256);
    const out = Buffer.from(result.cleanedBase64, 'base64');
    expect(out.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a');
  });

  it('trusts the bytes, not the declared mimetype: a PNG declared as SVG is stored as PNG', async () => {
    const png = await solidPng(64, 64, '#cc2222');
    const result = await processLogo(png, 'image/svg+xml');
    expect(result.mimetype).toBe('image/png');
  });

  it('trusts the bytes, not the declared mimetype: an SVG declared as PNG is still cleaned', async () => {
    const result = await processLogo(Buffer.from(EVIL_SVG, 'utf8'), 'image/png');
    expect(result.mimetype).toBe('image/svg+xml');
    expect(Buffer.from(result.cleanedBase64, 'base64').toString('utf8')).not.toMatch(/<script/i);
  });

  it('rejects an HTML document that claims to be an image', async () => {
    const html = Buffer.from('<html><body><script>alert(1)</script></body></html>', 'utf8');
    await expect(processLogo(html, 'image/png')).rejects.toBeInstanceOf(InvalidLogoError);
  });

  it('rejects random bytes', async () => {
    const junk = Buffer.from(Array.from({ length: 256 }, (_v, i) => (i * 37) % 251));
    await expect(processLogo(junk, 'image/png')).rejects.toBeInstanceOf(InvalidLogoError);
  });

  it('rejects an image whose decoded size would exhaust memory (decompression bomb)', async () => {
    // 5000 x 5000 = 25 megapixels: a few KB on the wire as a flat PNG, but
    // over the 24 megapixel decode limit.
    const bomb = await solidPng(5000, 5000, '#ffffff');
    expect(bomb.length).toBeLessThan(200_000);
    await expect(processLogo(bomb, 'image/png')).rejects.toBeInstanceOf(InvalidLogoError);
  });

  it('rejects a GIF even though sharp can read it (not in the allowed set)', async () => {
    const gif = await sharp({ create: { width: 8, height: 8, channels: 3, background: '#000' } }).gif().toBuffer();
    await expect(processLogo(gif, 'image/gif')).rejects.toBeInstanceOf(InvalidLogoError);
  });
});
