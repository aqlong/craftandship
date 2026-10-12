// Logo processing service: cleanup, resize, and color extraction
// Used during intake to derive the app's color scheme from an uploaded logo
//
// Two paths, chosen from the first bytes of the upload and never from the
// name or type the browser sent:
//  - PNG, JPEG and WebP go through the image library (sharp), which decodes
//    them under a pixel limit and re-encodes them as PNG.
//  - Anything that starts like an XML document is treated as an SVG and never
//    reaches the image library. It is rebuilt from an allowlist by
//    svg_sanitize.ts, in a worker thread, and its size and colors are read
//    from the rebuilt text. Drawing an SVG is the one step whose cost the
//    2 MB upload limit does not bound (a run of text, a filled path of many
//    curves or a chain of masks can hold a drawing library for minutes, or
//    overflow its stack and end the process), and nothing downstream needs a
//    drawing: the stored logo is the SVG itself, and the palette only has to
//    name colors.

import path from 'path';
import sharp from 'sharp';
import { ParseWorker } from './site_copy_crawl';
import { sanitizeSvg, type CleanedSvg } from './svg_sanitize';

// The cleaning itself is in svg_sanitize.ts (a file with no imports, so the
// worker thread that runs it loads nothing else). Re-exported because this
// module is where callers and tests look for it.
export { sanitizeSvg };

interface ColorScheme {
  primary_color: string;
  secondary_color: string;
  accent_color: string;
}

interface LogoResult {
  cleanedBase64: string;
  mimetype: string;
  colors: ColorScheme;
  width: number;
  height: number;
}

/** The upload is not a usable PNG, JPEG, WebP or SVG. The route maps this to HTTP 400. */
export class InvalidLogoError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'InvalidLogoError';
  }
}

const NOT_AN_ALLOWED_IMAGE = 'Only PNG, JPEG, SVG, and WebP images are allowed.';

/**
 * Largest upload this module reads. The intake route's own limit (multer) is
 * the same figure; it is repeated here so that the time and memory an upload
 * can cost do not depend on how a caller configured its parser.
 */
export const MAX_LOGO_BYTES = 2 * 1024 * 1024;

// ============================================================
// Raster uploads: PNG, JPEG, WebP
// ============================================================

// Decompression-bomb guard. A 2 MB upload can legitimately describe a 16k x 16k
// image (sharp's default limit is 268 megapixels, roughly 1 GB of decoded RGBA).
// 24 megapixels covers a 6000 x 4000 photo, far more than a logo needs. The
// limit is checked against the dimensions in the file's header, before any
// pixel is decoded.
const MAX_INPUT_PIXELS = 24_000_000;

type RasterFormat = 'png' | 'jpeg' | 'webp';

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/**
 * The raster format the first bytes announce, or null. Only an upload that
 * starts as one of these three is handed to the image library, so its other
 * decoders (GIF, TIFF, HEIF, SVG and the rest) never see uploaded bytes.
 */
function announcedRasterFormat(buffer: Buffer): RasterFormat | null {
  if (buffer.length >= 8 && buffer.subarray(0, 8).equals(PNG_SIGNATURE)) return 'png';
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) return 'jpeg';
  const isRiffWebp =
    buffer.length >= 12 && buffer.toString('latin1', 0, 4) === 'RIFF' && buffer.toString('latin1', 8, 12) === 'WEBP';
  return isRiffWebp ? 'webp' : null;
}

/**
 * Opens raster bytes with every limit stated, none left to a default:
 * at most `MAX_INPUT_PIXELS`; the library's own guards against memory
 * exhaustion on; one frame only, so an animated WebP costs what its first
 * frame costs; and any decoder warning (truncated or corrupt pixel data) is
 * an error, not something to draw around.
 */
function open(buffer: Buffer): ReturnType<typeof sharp> {
  return sharp(buffer, {
    limitInputPixels: MAX_INPUT_PIXELS,
    unlimited: false,
    failOn: 'warning',
    pages: 1,
    animated: false,
  });
}

async function processRaster(buffer: Buffer, announced: RasterFormat): Promise<LogoResult> {
  let outputBuffer: Buffer;
  try {
    // The decoder the library picked must be the one the first bytes named.
    const format = (await open(buffer).metadata()).format;
    if (format !== announced) throw new InvalidLogoError(NOT_AN_ALLOWED_IMAGE);
    // Rasters are re-encoded to PNG: this drops metadata (EXIF, GPS) and any
    // payload appended after the image data.
    outputBuffer = await open(buffer)
      .resize(512, 512, { fit: 'inside', withoutEnlargement: true })
      .png()
      .toBuffer();
  } catch (err) {
    if (err instanceof InvalidLogoError) throw err;
    throw new InvalidLogoError(`Could not read the image: ${(err as Error).message}`);
  }
  const outputMetadata = await open(outputBuffer).metadata();
  return {
    cleanedBase64: outputBuffer.toString('base64'),
    mimetype: 'image/png',
    colors: await extractColors(outputBuffer),
    width: outputMetadata.width || 0,
    height: outputMetadata.height || 0,
  };
}

// ============================================================
// SVG uploads
// ============================================================

/** Longest one SVG cleaning may take, starting the thread included, before the thread is ended. */
const SVG_CLEAN_TIMEOUT_MS = 5000;

/** SVG uploads that may be in line for the cleaning thread at once, the one being cleaned included. */
const MAX_SVG_CLEANS_IN_LINE = 8;

/**
 * More SVG uploads are already in line to be cleaned than this server takes
 * on. Nothing is wrong with the upload: the same file works a moment later.
 */
export class LogoBusyError extends Error {
  constructor() {
    super('Too many logos are being processed right now. Please try again in a moment.');
    this.name = 'LogoBusyError';
  }
}

/** For tests: a different worker entry (one that never answers, say) and a shorter limit. */
export interface SvgCleanOptions {
  workerFile?: string;
  timeoutMs?: number;
}

/**
 * What this module uses of the crawler's `ParseWorker`: a thread that takes
 * one job, answers within a time limit or is terminated, and can be closed.
 * The class posts whatever job it is given and resolves with the `result`
 * field of the entry's reply; only its type signature names the crawler's own
 * jobs, which is why `startSvgCleanThread` casts.
 */
interface SvgCleanThread {
  run(svg: string, timeoutMs: number): Promise<CleanedSvg | null>;
  close(): Promise<void>;
}

/** The worker entry that sits next to this file: .js when compiled, .ts from source. */
function defaultSvgWorkerEntry(): string {
  return path.join(__dirname, `svg_sanitize_worker${path.extname(__filename)}`);
}

function startSvgCleanThread(entryFile: string): SvgCleanThread {
  return new ParseWorker(entryFile) as unknown as SvgCleanThread;
}

async function cleanInOneThread(svg: string, options: SvgCleanOptions): Promise<CleanedSvg | null> {
  const thread = startSvgCleanThread(options.workerFile ?? defaultSvgWorkerEntry());
  try {
    return await thread.run(svg, options.timeoutMs ?? SVG_CLEAN_TIMEOUT_MS);
  } finally {
    await thread.close();
  }
}

// One cleaning thread at a time, in arrival order: a burst of uploads then
// costs one extra thread, not one each.
let svgCleanQueue: Promise<void> = Promise.resolve();
let svgCleansInLine = 0;

/**
 * `cleanSvg(svg)` (svg_sanitize.ts: the rebuilt document with its size and
 * colors, or null when the text is refused), computed in a worker thread so
 * that the server's event loop is free while it runs (one web process serves
 * everything, the companion's crisis handler included). Cleaning takes time
 * proportional to the length of the text (that file says how); the thread is
 * what still holds if a later change to it does not. A cleaning that has not
 * answered within the time limit is ended with `worker.terminate()` and this
 * rejects, as it does when the thread fails or cannot be started. Rejects
 * with `LogoBusyError`, without starting anything, when
 * `MAX_SVG_CLEANS_IN_LINE` uploads are already in line.
 */
export async function cleanSvgOffLoop(svg: string, options: SvgCleanOptions = {}): Promise<CleanedSvg | null> {
  if (svgCleansInLine >= MAX_SVG_CLEANS_IN_LINE) throw new LogoBusyError();
  svgCleansInLine += 1;
  const cleaned = svgCleanQueue.then(() => cleanInOneThread(svg, options));
  svgCleanQueue = cleaned.then(
    () => undefined,
    () => undefined
  );
  try {
    return await cleaned;
  } finally {
    svgCleansInLine -= 1;
  }
}

/**
 * True when the upload starts the way an XML document does: an optional
 * UTF-8 byte order mark, optional white space, then "<". No PNG, JPEG or WebP
 * starts like that, and no XML document starts any other way, so this decides
 * which uploads are kept away from the image library.
 */
function startsLikeXml(buffer: Buffer): boolean {
  let index = buffer.length >= 3 && buffer[0] === 0xef && buffer[1] === 0xbb && buffer[2] === 0xbf ? 3 : 0;
  while (
    index < buffer.length &&
    (buffer[index] === 0x20 || buffer[index] === 0x09 || buffer[index] === 0x0a || buffer[index] === 0x0d)
  ) {
    index += 1;
  }
  return buffer[index] === 0x3c;
}

/**
 * The upload as text. Only UTF-8 (which includes plain ASCII) is read: the
 * cleaned file is written as UTF-8 and says so, and bytes that are not UTF-8
 * would otherwise become replacement characters, three bytes for each one.
 */
function decodeSvgText(buffer: Buffer): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buffer);
  } catch {
    throw new InvalidLogoError('SVG logos must be saved as UTF-8 text.');
  }
}

/**
 * Vector logos stay vector: what is stored is a new document written from
 * the listed parts of the upload. Its size comes from the root element and
 * its palette from the colors it names; it is not drawn.
 */
async function processSvg(buffer: Buffer): Promise<LogoResult> {
  const cleaned = await cleanSvgOffLoop(decodeSvgText(buffer));
  if (!cleaned) throw new InvalidLogoError(NOT_AN_ALLOWED_IMAGE);
  const counts: ColorCounts = new Map();
  for (const color of cleaned.colors) countColor(counts, color.red, color.green, color.blue, color.count);
  return {
    cleanedBase64: Buffer.from(cleaned.svg, 'utf8').toString('base64'),
    mimetype: 'image/svg+xml',
    colors: paletteFromCounts(counts),
    width: cleaned.width,
    height: cleaned.height,
  };
}

/**
 * Process an uploaded logo image: resize to max 512px, optimize,
 * and extract dominant colors for the app's color scheme.
 *
 * The declared mimetype is accepted only for backwards compatibility with the
 * caller; the real type comes from the bytes. Throws InvalidLogoError when the
 * bytes are not a supported image.
 */
export async function processLogo(buffer: Buffer, _declaredMimetype?: string): Promise<LogoResult> {
  if (buffer.length > MAX_LOGO_BYTES) {
    throw new InvalidLogoError('That image is larger than 2 MB. Please choose a smaller file.');
  }
  if (startsLikeXml(buffer)) return processSvg(buffer);
  const announced = announcedRasterFormat(buffer);
  if (!announced) throw new InvalidLogoError(NOT_AN_ALLOWED_IMAGE);
  return processRaster(buffer, announced);
}

// ============================================================
// Palette
// ============================================================

const DEFAULT_PALETTE: ColorScheme = {
  primary_color: '#2563eb',
  secondary_color: '#1e40af',
  accent_color: '#f59e0b',
};

/** "r,g,b" of a color bucket to how much of it was seen, in order of first appearance. */
type ColorCounts = Map<string, number>;

/** Colors are grouped into buckets of this width per channel, to reduce noise. */
const COLOR_BUCKET = 32;

/**
 * Adds `weight` to the bucket of one color, unless it is near white, near
 * black or a gray: those say nothing about a brand's palette.
 */
function countColor(counts: ColorCounts, red: number, green: number, blue: number, weight: number): void {
  const r = Math.round(red / COLOR_BUCKET) * COLOR_BUCKET;
  const g = Math.round(green / COLOR_BUCKET) * COLOR_BUCKET;
  const b = Math.round(blue / COLOR_BUCKET) * COLOR_BUCKET;

  // Skip near-white and near-black
  const brightness = (r + g + b) / 3;
  if (brightness > 240 || brightness < 15) return;

  // Skip very desaturated colors (grays)
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max - min < 20 && brightness > 50 && brightness < 200) return;

  const key = `${r},${g},${b}`;
  counts.set(key, (counts.get(key) || 0) + weight);
}

/**
 * The three most frequent colors as the scheme, most frequent first (equal
 * counts keep their order of first appearance). With fewer than three the
 * missing ones are derived from the first, and with none the default blue
 * theme is returned.
 */
function paletteFromCounts(counts: ColorCounts): ColorScheme {
  const sorted = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([key]) => {
      const [r, g, b] = key.split(',').map(Number);
      return rgbToHex(r, g, b);
    });

  if (sorted.length >= 3) {
    return { primary_color: sorted[0], secondary_color: sorted[1], accent_color: sorted[2] };
  }
  if (sorted.length === 2) {
    return { primary_color: sorted[0], secondary_color: sorted[1], accent_color: generateAccent(sorted[0]) };
  }
  if (sorted.length === 1) {
    return {
      primary_color: sorted[0],
      secondary_color: darken(sorted[0], 20),
      accent_color: generateAccent(sorted[0]),
    };
  }
  return { ...DEFAULT_PALETTE };
}

/**
 * Dominant colors of a raster image (the PNG this module has just written,
 * at most 512 pixels a side): sample it at 64 x 64 and count the pixels in
 * each color bucket.
 */
async function extractColors(buffer: Buffer): Promise<ColorScheme> {
  try {
    // Get raw pixel data (resize to small for speed)
    const { data } = await open(buffer)
      .resize(64, 64, { fit: 'cover' })
      .removeAlpha()
      .raw()
      .toBuffer({ resolveWithObject: true });

    const counts: ColorCounts = new Map();
    for (let i = 0; i < data.length; i += 3) {
      countColor(counts, data[i], data[i + 1], data[i + 2], 1);
    }
    return paletteFromCounts(counts);
  } catch (err) {
    // The logo is still usable without a palette taken from it.
    console.error('Logo color extraction failed, using the default palette:', (err as Error).message);
    return { ...DEFAULT_PALETTE };
  }
}

function rgbToHex(r: number, g: number, b: number): string {
  const clamp = (n: number) => Math.max(0, Math.min(255, n));
  return '#' + [clamp(r), clamp(g), clamp(b)]
    .map(c => c.toString(16).padStart(2, '0'))
    .join('');
}

function hexToRgb(hex: string): { r: number; g: number; b: number } {
  const clean = hex.replace('#', '');
  return {
    r: parseInt(clean.substring(0, 2), 16),
    g: parseInt(clean.substring(2, 4), 16),
    b: parseInt(clean.substring(4, 6), 16),
  };
}

function darken(hex: string, amount: number): string {
  const { r, g, b } = hexToRgb(hex);
  return rgbToHex(
    Math.max(0, r - amount),
    Math.max(0, g - amount),
    Math.max(0, b - amount),
  );
}

function generateAccent(hex: string): string {
  // Generate a complementary accent by shifting hue ~120 degrees
  const { r, g, b } = hexToRgb(hex);

  // Simple hue rotation: swap channels and adjust
  const accent_r = Math.min(255, b + 40);
  const accent_g = Math.min(255, r + 20);
  const accent_b = Math.max(0, g - 20);

  return rgbToHex(accent_r, accent_g, accent_b);
}
