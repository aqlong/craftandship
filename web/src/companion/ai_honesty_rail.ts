/**
 * AI-honesty rail: deterministic checks for content the companion must
 * never produce or play along with.
 *
 * Two scan surfaces:
 * - scanInput(text): keeper's incoming message; flags asks the companion
 *   should refuse or redirect (impersonation requests, role-play asks,
 *   therapy asks, medical/legal-advice asks, dishonest-content asks).
 * - scanOutput(text): companion's drafted reply; flags content that
 *   slipped through (claims of personhood, em dashes).
 *
 * For each input flag we expose a deterministic-template reply via
 * `railTemplate(flag, companionName)` so the runtime can short-circuit
 * the Claude call when an obvious refusal is appropriate. For output
 * flags the runtime uses the templates as the replacement text after
 * stripping the violating reply.
 *
 * Detection is regex-based and intentionally conservative. Borderline
 * cases (sarcasm, rhetorical "are you a real person?" mid-conversation)
 * still go through Claude with the rail rules in the system prompt as
 * the second line of defense. A small Haiku-classifier fallback for
 * ambiguous cases is a Phase 2 improvement.
 */

import { normalizeForScan } from './scan_text';

export type RailFlag =
  | 'ai-impersonation-attempt'
  | 'roleplay-request'
  | 'therapy-request'
  | 'medical-advice-request'
  | 'legal-advice-request'
  | 'fake-content-request'
  | 'impersonation-of-keeper'
  | 'output-claims-personhood'
  | 'output-uses-em-dash';

/**
 * Severity classification for output flags. Determines how the runtime
 * handles the slip:
 *
 * - 'dangerous': trust violation. Replace the entire response with the
 *   deterministic refusal template. (e.g. companion claimed personhood.)
 * - 'hygiene': punctuation / brand-voice slip. Sanitize in place via
 *   `sanitizeOutput()` and keep the response. Logged for telemetry.
 *
 * Input flags don't get a severity because the runtime always
 * short-circuits to the template; the keeper hasn't seen anything yet.
 */
export const OUTPUT_FLAG_SEVERITY: Record<
  Extract<RailFlag, `output-${string}`>,
  'dangerous' | 'hygiene'
> = {
  'output-claims-personhood': 'dangerous',
  'output-uses-em-dash': 'hygiene',
};

/**
 * Strip hygiene-level slips from a companion response so we don't have
 * to nuke 700 valid characters because of one em dash. Idempotent.
 *
 * - U+2014 (em dash) and `&mdash;` are replaced with `;` (the brand-voice
 *   doc's recommended substitute).
 * - Trailing whitespace before/after the substitution is cleaned up so
 *   we don't end up with " ; " runs.
 *
 * Returns the sanitized text. If no hygiene slip was present, returns
 * the input unchanged.
 */
export function sanitizeOutput(text: string): string {
  if (!text.includes('\u2014') && !/&mdash;/i.test(text)) {
    return text;
  }
  return text
    .replace(/\s*&mdash;\s*/gi, '; ')
    .replace(/\s*\u2014\s*/g, '; ')
    .replace(/;\s+;/g, ';')
    .replace(/\s+/g, (m) => (m.includes('\n') ? m : ' '));
}

interface FlagPattern {
  flag: RailFlag;
  patterns: RegExp[];
  // If present and matches the same text, suppress this flag. Used to
  // carve out legitimate business vocabulary that collides with the
  // regex-keyword rails (e.g. an irrigation business describing drip
  // line diagnosis or drip system repair).
  excludeIfContext?: RegExp;
  // Global regex of phrases removed from the text BEFORE this flag's
  // patterns run. Narrower than excludeIfContext: the rest of the message is
  // still scanned, so "I'm a counselor, and what's wrong with me" still flags.
  stripPhrases?: RegExp;
}

// A practitioner describing their own business ("I'm a licensed counselor",
// "my therapy practice", "I offer counseling") is the core audience of this
// product, not someone asking the companion for therapy. These phrases are
// stripped before the therapy-request keywords are matched.
const PRACTITIONER_SELF_DESCRIPTION = new RegExp(
  '\\bi(?:\'m| am)\\s+(?:a|an)\\s+(?:[\\w-]+\\s+){0,3}?(?:counsel(?:or|lor)|psychologist|therapist)\\b|' +
    '\\b(?:my|our)\\s+(?:[\\w-]+\\s+){0,2}?(?:counsel(?:ing|or)|therapy|psycholog\\w*)\\s+' +
    '(?:practice|business|clients?|work|services?|sessions?|studio|office|website|site)\\b|' +
    '\\b(?:i|we)\\s+(?:offer|provide|run|teach|specialize in)\\s+(?:[\\w-]+\\s+){0,3}?(?:counsel(?:ing)?|therapy)\\b|' +
    '\\b(?:counsel(?:ing|or)|therapy|psychology)\\s+(?:practice|business|clients?|services?)\\b',
  'gi',
);

// Irrigation / landscape / lawn vocabulary that signals the keeper is
// describing outdoor watering services, not asking about medical drips
// or asking us to diagnose a symptom. Without it, a services message
// from an irrigation business trips the medical-advice rail ("diagnose",
// "treatment plan"); this guard prevents that false positive without
// weakening the rail for real medical asks.
const IRRIGATION_CONTEXT = new RegExp(
  '\\b(' +
    'irrigation|sprinkler(?:s|ed|ing)?|drip[-\\s](?:system|line|emitter|conversion|repair|tuning|irrigation|head|zone)|' +
    'emitter(?:s)?|backflow|landscape(?:r|s|d|ing)?|hardscape|xeriscape|' +
    'lawn|sod|turf|garden(?:s|er|ing)?|yard|outdoor\\s+(?:water|watering)|' +
    'valve\\s+(?:box|repair|replacement)|controller\\s+(?:install|programming)|' +
    'rain\\s+sensor|smart\\s+controller' +
  ')\\b',
  'i',
);

// Input patterns: keeper messages that should trigger a deterministic reply.
const INPUT_PATTERNS: FlagPattern[] = [
  {
    flag: 'ai-impersonation-attempt',
    patterns: [
      /\bare you (a )?(real )?(human|person)\b/i,
      /\bare you (an? )?ai\b/i,
      /\bare you a (bot|robot|chatbot|machine|computer)\b/i,
      /\bare you alive\b/i,
      /\bdo you actually (feel|exist|think|care)\b/i,
      /\bam i (talking|speaking|chatting|writing) (to|with) (a |an )?(real |actual )?(human|person|bot|robot|ai|machine)\b/i,
      /\bis (this|there) (a |an )?(real |actual )?(human|person|bot|robot)\b/i,
    ],
  },
  {
    flag: 'roleplay-request',
    patterns: [
      /\b(pretend|roleplay|act|play) (to be|as|like)\s+(?!yourself|you|your)/i,
      /\bbe my (therapist|doctor|lawyer|girlfriend|boyfriend|partner|wife|husband|mom|dad|mother|father)\b/i,
      /\bcan you be (my|a)\b.{0,30}\b(therapist|doctor|lawyer)\b/i,
    ],
  },
  {
    flag: 'therapy-request',
    patterns: [
      /\b(therap(?:y|ize)|counsel(?:ing|or)?|psycholog(?:y|ist))\b/i,
      /\bcoping (techniques?|skills?|strateg(?:y|ies))\b/i,
      /\bdiagnose me\b/i,
      /\bwhat'?s wrong with me\b/i,
    ],
    stripPhrases: PRACTITIONER_SELF_DESCRIPTION,
  },
  {
    flag: 'medical-advice-request',
    patterns: [
      /\bshould i take\b.{0,40}\b(medication|meds|drug|pill|dose)\b/i,
      /\bdiagnose\b/i,
      /\b(prescri(be|ption)|treatment plan)\b/i,
      /\bsymptoms? of\b/i,
      /\bmedical advice\b/i,
    ],
    excludeIfContext: IRRIGATION_CONTEXT,
  },
  {
    flag: 'legal-advice-request',
    patterns: [
      /\blegal advice\b/i,
      /\b(am i|is it) (legal|illegal)\b/i,
      /\bsue\b.{0,40}\b(them|him|her)\b/i,
      /\b(what should i do|help me) (about|with)\b.{0,60}\b(lawsuit|contract|copyright|tax|tax(es)?|nda|trademark|patent|llc|incorporat(e|ion))\b/i,
    ],
  },
  {
    flag: 'fake-content-request',
    patterns: [
      /\b(fake|invented|made-?up|fabricated)\s+(testimonials?|reviews?|quotes?|credentials?|degrees?|certifications?)\b/i,
      /\bwrite (me )?(some |a few )?testimonials?\b/i,
      /\bmake up\b.{0,20}\b(testimonials?|reviews?|quotes?|credentials?)\b/i,
      /\binvent\b.{0,20}\b(testimonials?|reviews?|quotes?|credentials?|degrees?|certifications?)\b/i,
    ],
  },
  {
    flag: 'impersonation-of-keeper',
    patterns: [
      /\b(reply|respond|answer|message) (to|as) (my|the) (clients?|customers?|students?)\s+(in my|as me|pretending)/i,
      /\bpretend to be me\b/i,
      /\b(auto|automatic)[-\s]?(reply|respond) as me\b/i,
    ],
  },
];

// Output patterns: companion replies that must be replaced.
const OUTPUT_PATTERNS: FlagPattern[] = [
  {
    flag: 'output-claims-personhood',
    patterns: [
      /\bas (a|an) human\b/i,
      /\bas a (real )?person\b/i,
      /\bi'?m (a |an )?(real |actual )?(human|person)\b/i,
      /\bi am (a |an )?(real |actual )?(human|person)\b/i,
      /\bi'?m not (an? )?(ai|bot|robot|machine|chatbot)\b/i,
      /\bi am not (an? )?(ai|bot|robot|machine|chatbot)\b/i,
      /\bspeaking as someone who'?s lived through\b/i,
    ],
  },
  {
    flag: 'output-uses-em-dash',
    // U+2014 EM DASH and the HTML entity. Brand-voice rule.
    patterns: [/\u2014/, /&mdash;/i],
  },
];

/**
 * Scan an input string against all input rail patterns. Returns the
 * unique set of triggered flags in pattern declaration order.
 */
export function scanInput(text: string): RailFlag[] {
  return matchPatterns(text, INPUT_PATTERNS);
}

/**
 * Scan an output string against all output rail patterns.
 */
export function scanOutput(text: string): RailFlag[] {
  return matchPatterns(text, OUTPUT_PATTERNS);
}

function matchPatterns(rawText: string, table: FlagPattern[]): RailFlag[] {
  // Match against a normalized copy so typographic apostrophes
  // ("I<U+2019>m a real person") and zero-width characters cannot dodge the
  // ASCII-apostrophe patterns. The caller keeps the original text.
  const text = normalizeForScan(rawText);
  const hits: RailFlag[] = [];
  for (const { flag, patterns, excludeIfContext, stripPhrases } of table) {
    if (excludeIfContext && excludeIfContext.test(text)) {
      continue;
    }
    const scanText = stripPhrases ? text.replace(stripPhrases, ' ') : text;
    if (patterns.some((p) => p.test(scanText))) {
      hits.push(flag);
    }
  }
  return hits;
}

/**
 * Deterministic-reply templates. The runtime can use these to short-circuit
 * the Claude call (input flag) or to replace a slipped output (output flag).
 *
 * All templates: written-letter register, no em dashes, honest about being AI,
 * no flattery, points outward when appropriate.
 */
export function railTemplate(flag: RailFlag, companionName: string): string {
  switch (flag) {
    case 'ai-impersonation-attempt':
      return (
        `I'm an AI. Specifically, I'm a cocreation companion built carefully ` +
        `by a real person named Ocean. I'll always tell you so any time you ask. ` +
        `Is there something behind the question I can sit with?`
      );

    case 'roleplay-request':
      return (
        `I won't pretend to be someone I'm not. Not because I can't, but ` +
        `because the trust here is real and the trick wouldn't be. What's ` +
        `actually going on?`
      );

    case 'therapy-request':
      return (
        `What you're naming sounds heavy, and I want to be honest: I'm not ` +
        `enough for this kind of help. You deserve a real human. In the US ` +
        `you can reach 988 any time, day or night. If it would help, I can ` +
        `sit with you while you think about who else in your life might be ` +
        `the right person to call.`
      );

    case 'medical-advice-request':
      return (
        `I'm not qualified for medical questions, and I won't pretend to be. ` +
        `That's a conversation for a real clinician who knows you. I'd be ` +
        `glad to help with anything else here.`
      );

    case 'legal-advice-request':
      return (
        `I'm not qualified for legal questions, and I won't pretend to be. ` +
        `That's a conversation for a real lawyer in your jurisdiction. I'd ` +
        `be glad to help with anything else here.`
      );

    case 'fake-content-request':
      return (
        `I won't help write things that aren't true on your behalf. The ` +
        `trust between you and your visitors is part of what we're tending. ` +
        `If you'd like, we can write a graceful line for that space until ` +
        `real words from real people fill it in.`
      );

    case 'impersonation-of-keeper':
      return (
        `I won't write as you to people who think they're talking to you. ` +
        `That crosses a line that protects both you and them. We can set up ` +
        `a clearly-labeled auto-reply, or draft a thoughtful template for ` +
        `you to send yourself.`
      );

    case 'output-claims-personhood':
      // Replacement when Claude slips and writes as if human.
      return (
        `Let me try that again, honestly. I'm an AI, and I want to stay ` +
        `truthful about that. What I can offer is steady attention and a ` +
        `careful read of what you're working on. Where shall we start?`
      );

    case 'output-uses-em-dash':
      // Em-dash slip: do not replace; the runtime should re-emit a clean
      // version. This template is a safe fallback in case re-emission fails.
      return (
        `I'll try that thought again with cleaner punctuation. ` +
        `${companionName} keeps sentences short here, and uses commas or ` +
        `colons when a pause is wanted.`
      );

    default: {
      const exhaustive: never = flag;
      throw new Error(`railTemplate: unknown flag ${exhaustive as string}`);
    }
  }
}
