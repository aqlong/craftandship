// The web tier's copy of the worker's parser for on/off environment variables
// (coordinator/envflags.py). The admin settings page shows, for every switch it
// can write, the state the worker will read from the stored text, so the two
// parsers have to agree word for word. tests/unit/env-switch.test.ts reads the
// Python file and compares the word lists (and, when a python3 is on PATH,
// runs both parsers over the same values); tests/test_envflags.py reads this
// file from the Python side.

export const ON_WORDS: readonly string[] = ['true', '1', 'yes', 'on', 'enabled'];
export const OFF_WORDS: readonly string[] = ['false', '0', 'no', 'off', 'disabled'];

// What Python's str.strip() removes. String.prototype.trim() is not the same
// set: it also removes U+FEFF and leaves U+001C to U+001F and U+0085.
const PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const PY_STRIP = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'g');

function word(raw: string): string {
  return raw.replace(PY_STRIP, '').toLowerCase();
}

/**
 * A switch whose ON state is the safe one (a dry run). Unset or blank reads
 * as `unset`; an OFF word reads as off; every other text, a typo included,
 * reads as on.
 */
export function safetySwitch(raw: string | undefined, unset = true): boolean {
  const text = word(raw ?? '');
  if (!text) return unset;
  return !OFF_WORDS.includes(text);
}

/**
 * A switch whose ON state arms something (start the coordinator, run an
 * agent). Unset reads as `unset`; an ON word reads as on; every other text,
 * blank or a typo included, reads as off.
 */
export function armingSwitch(raw: string | undefined, unset = false): boolean {
  if (raw === undefined) return unset;
  return ON_WORDS.includes(word(raw));
}
