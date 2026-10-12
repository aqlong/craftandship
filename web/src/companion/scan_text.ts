/**
 * Text normalization shared by the regex rails (crisis_detection.ts and
 * ai_honesty_rail.ts).
 *
 * The rail patterns are written with a plain ASCII apostrophe ("don't",
 * "I'm"). Phone keyboards and word processors emit typographic
 * apostrophes (U+2019 and friends) by default, and a model reply may too,
 * so without this step "I don<U+2019>t want to live anymore" slips past
 * every apostrophe-bearing pattern. Zero-width characters and soft
 * hyphens are stripped for the same reason ("kill my<ZWSP>self").
 *
 * Normalization is for MATCHING only. Callers must keep scanning the
 * normalized copy and persist / display the original text unchanged.
 */

/** Typographic apostrophes and look-alikes mapped to a plain apostrophe. */
const APOSTROPHE_LIKE = /[\u2018\u2019\u201B\u02BC\u02B9\u2032`\u00B4]/g;

/** Zero-width space/joiners, word joiner, BOM, soft hyphen. */
const INVISIBLE = /[\u200B-\u200D\u2060\uFEFF\u00AD]/g;

export function normalizeForScan(text: string): string {
  return text.normalize('NFKC').replace(APOSTROPHE_LIKE, "'").replace(INVISIBLE, '');
}
