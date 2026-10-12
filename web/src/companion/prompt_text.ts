/**
 * Helpers for interpolating stored, model-derived or user-typed text into
 * LLM prompts.
 *
 * Memory rows hold text a keeper typed (`keeper_phrase`, verbatim) or text a
 * classifier model wrote about it (`content`). Both are rendered into the
 * system block of later turns. Pasting them in raw lets a stored value
 * contain newlines and forge structure inside the prompt ("\n\n## New
 * instructions ..."), or close the surrounding quote early. This is prompt
 * injection hygiene, not a defence against it: the text still reaches the
 * model, and a keeper can still influence their own conversation. What it
 * prevents is a value escaping its line. The blast radius of an injected row
 * is one keeper's own conversation: rows are scoped by trial_id / keeper_id,
 * and nothing the model emits can dispatch a build without the separate,
 * authenticated /lighthouse/build or admin step.
 */

/**
 * Collapse all whitespace (including newlines) to single spaces, trim, and
 * cap the length. Output is always a single line.
 */
export function oneLine(text: string | null | undefined, maxChars: number): string {
  const flat = String(text ?? '').replace(/\s+/g, ' ').trim();
  if (flat.length <= maxChars) return flat;
  return flat.slice(0, Math.max(0, maxChars - 3)).trimEnd() + '...';
}

/**
 * A verbatim quote destined for a double-quoted span: single line, capped,
 * and inner double quotes turned into single quotes so the span cannot be
 * closed early.
 */
export function quotedSpan(text: string | null | undefined, maxChars: number): string {
  return oneLine(text, maxChars).replace(/"/g, "'");
}

export const MEMORY_CONTENT_MAX_CHARS = 500;
export const MEMORY_PHRASE_MAX_CHARS = 300;

/**
 * One bullet for a memory row: `- [category] content (their words: "...")`.
 * Shared by the companion's per-keeper prompt, spec extraction and the
 * conversion letter so all three treat stored text identically.
 */
export function renderMemoryLine(row: {
  category: string;
  content: string;
  keeper_phrase: string | null;
}): string {
  const phrase = row.keeper_phrase
    ? ` (their words: "${quotedSpan(row.keeper_phrase, MEMORY_PHRASE_MAX_CHARS)}")`
    : '';
  return `- [${oneLine(row.category, 40)}] ${oneLine(row.content, MEMORY_CONTENT_MAX_CHARS)}${phrase}`;
}
