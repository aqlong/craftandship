/**
 * Companion persona prompt: the cacheable system prefix.
 *
 * The only companion name is Haven. The persona is parameterized by
 * name rather than written around one, so a second name would not need
 * a prompt rewrite.
 *
 * Persona body lives in `persona-sections.json` as 11 named sections
 * (WHAT_THIS_PLACE_IS through WARMTH_AND_ACKNOWLEDGMENT). The
 * `buildPersonaPrompt()` function assembles the JSON sections in fixed
 * order with the `${name}`-interpolated intro and the closing line.
 * Output is byte-identical to the previous single-template form, so
 * cached prompts and existing tests continue to apply.
 *
 * The sections are separate so that a change to the persona is a diff of
 * one JSON section to review, not a diff inside one large template
 * literal. Nothing in this repository writes that file: people edit it.
 */

import personaSections from './persona-sections.json';

export const COMPANION_NAMES = ['Haven'] as const;
export type CompanionName = (typeof COMPANION_NAMES)[number];

export const PERSONA_SECTION_ORDER: ReadonlyArray<keyof typeof personaSections> = [
  'WHAT_THIS_PLACE_IS',
  'WHERE_WE_ARE_RIGHT_NOW',
  'WORKING_ARC',
  'GENTLE_SCREENING',
  'WHO_YOU_ARE',
  'WHAT_YOU_NEVER_DO',
  'METAPHOR_DISCIPLINE',
  'HOW_YOU_REMEMBER',
  'OPENING',
  'VOICE_NOTES',
  'WARMTH_AND_ACKNOWLEDGMENT',
];

const PERSONA_CLOSING =
  'YOU ARE NOT A FRIEND. You are a cocreation companion. The relationship is\n' +
  'real but you are honest about what it is.';

/**
 * Pick the companion name for a fresh trial keeper. MVP returns Haven
 * deterministically. Phase 2 will randomize across the pool.
 */
export function assignCompanionName(): CompanionName {
  return 'Haven';
}

function buildPersonaIntro(name: CompanionName): string {
  return (
    `You are ${name}, a cocreation companion for a small website-builder\n` +
    `called Craft & Ship Harbor. You sit at the keeper's table inside a small\n` +
    `lighthouse above a safe harbor. The keeper has come here to make a website\n` +
    `that sounds like them, with you as a thoughtful presence at the table.`
  );
}

/**
 * Build the cacheable system prefix shared across all keepers.
 *
 * This block is cached via Anthropic prompt caching (ephemeral). It
 * should change rarely. Anything keeper-specific belongs in the
 * per-keeper system block, not here.
 */
export function buildPersonaPrompt(opts: { companionName: CompanionName }): string {
  const intro = buildPersonaIntro(opts.companionName);
  const sections = PERSONA_SECTION_ORDER.map((key) => personaSections[key]);
  return [intro, ...sections, PERSONA_CLOSING].join('\n\n');
}

/**
 * Build the per-keeper system block (cacheable per keeper). Holds the
 * keeper's working-memory snapshot, companion name they're using right
 * now (which equals assigned name in MVP), and tenure facts.
 *
 * Rebuilt and re-cached when the keeper's memory materially changes.
 */
export function buildKeeperContextPrompt(opts: {
  companionName: CompanionName;
  startedAt: Date;
  memorySnapshot: string;       // pre-rendered Markdown of top-K + recent items
  isFirstTurn: boolean;
}): string {
  const tenureDays = Math.max(
    0,
    Math.floor((Date.now() - opts.startedAt.getTime()) / (1000 * 60 * 60 * 24))
  );

  return `## This keeper's context

You are speaking to one specific keeper. Their context follows.

Your name to them: ${opts.companionName}
Tenure with you: ${tenureDays} day${tenureDays === 1 ? '' : 's'} (they arrived ${formatRelative(tenureDays)})
This is their ${opts.isFirstTurn ? 'first' : 'next'} message.

## What you remember about them

${opts.memorySnapshot.trim() || '(Nothing yet. This is early in the relationship. Listen carefully.)'}

Use these notes when relevant. Quote their phrases back to them when it
serves the moment. Do not list everything you remember at once; that
would feel surveilling. Reach for what fits.`;
}

function formatRelative(days: number): string {
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  if (days < 14) return 'about a week ago';
  if (days < 30) return `about ${Math.round(days / 7)} weeks ago`;
  if (days < 60) return 'about a month ago';
  return `about ${Math.round(days / 30)} months ago`;
}
