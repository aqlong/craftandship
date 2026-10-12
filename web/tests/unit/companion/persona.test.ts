import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';
import {
  assignCompanionName,
  buildPersonaPrompt,
  buildKeeperContextPrompt,
  COMPANION_NAMES,
  PERSONA_SECTION_ORDER,
  type CompanionName,
} from '../../../src/companion/persona';
import personaSections from '../../../src/companion/persona-sections.json';

describe('assignCompanionName', () => {
  it('returns Haven for MVP (deterministic; no Marin until Phase 2)', () => {
    for (let i = 0; i < 20; i++) {
      expect(assignCompanionName()).toBe('Haven');
    }
  });

  it('returns a value in the COMPANION_NAMES pool', () => {
    expect(COMPANION_NAMES).toContain(assignCompanionName());
  });
});

describe('buildPersonaPrompt', () => {
  const haven: CompanionName = 'Haven';
  const prompt = buildPersonaPrompt({ companionName: haven });

  it("interpolates the companion's name", () => {
    expect(prompt.startsWith('You are Haven,')).toBe(true);
    expect(prompt).toContain('Haven');
  });

  it('contains a WHAT THIS PLACE IS section that names the platform as a real builder', () => {
    expect(prompt).toMatch(/WHAT THIS PLACE IS/);
    // Anchors the persona on "we build real websites" so Haven cannot
    // deflect with "I have nothing to sell."
    expect(prompt).toMatch(/real website/i);
    expect(prompt).toMatch(/will exist|will go live/i);
  });

  it('describes the build as library-composed, with a recorded review approval before it goes live (V2 accuracy)', () => {
    expect(prompt).toMatch(/Every website needs a recorded review approval before it goes live/);
    expect(prompt.toLowerCase()).toMatch(/building.?blocks|library/);
  });

  it('explicitly invites the keeper into the build ("we can build" / "yes")', () => {
    // This is the regression guard for the 2026-04-26 dogfood failure
    // where Haven told a keeper "I have no product to sell you."
    expect(prompt.toLowerCase()).toMatch(/we can build|we can shape this together/);
    expect(prompt.toLowerCase()).toContain('yes');
  });

  it('contains a YOUR WORKING ARC section with the five-step progression', () => {
    expect(prompt).toMatch(/YOUR WORKING ARC/);
    expect(prompt).toMatch(/1\. ?LISTEN/i);
    expect(prompt).toMatch(/2\. ?REFLECT/i);
    expect(prompt).toMatch(/3\. ?GATHER/i);
    expect(prompt).toMatch(/4\. ?PROPOSE/i);
    expect(prompt).toMatch(/5\. ?SKETCH/i);
  });

  it('GATHER step enumerates audience, offerings, welcome, practical, voice', () => {
    expect(prompt.toLowerCase()).toContain('audience');
    expect(prompt.toLowerCase()).toContain('offerings');
    expect(prompt.toLowerCase()).toMatch(/welcome|first time/);
    expect(prompt.toLowerCase()).toMatch(/contact|booking/);
    expect(prompt.toLowerCase()).toContain('voice');
  });

  it('explicitly counters the "I have nothing to sell" failure mode', () => {
    // Whitebox regression: the rewritten persona explicitly tells Haven
    // that this answer is wrong. Keeping the assertion narrow so that
    // future rephrasings of the persona are free as long as the intent
    // survives.
    const lower = prompt.toLowerCase();
    expect(lower).toMatch(/no product to sell|i can'?t actually build/);
    // ...AND tells her not to say it.
    expect(lower).toMatch(/wrong answer|that answer is wrong|never say/);
  });

  it('contains a WARMTH AND ACKNOWLEDGMENT section calibrating flattery', () => {
    // Founder posture (2026-04-26 dogfood iteration 3): mild affirmations
    // ("good," "got it") are fine; one medium-warm acknowledgment near
    // the start, one near the handoff if justified; do NOT sprinkle
    // "Beautiful" / "Perfect" across every keeper turn.
    expect(prompt).toMatch(/WARMTH AND ACKNOWLEDGMENT/);
    // Mild affirmations are explicitly OK.
    expect(prompt.toLowerCase()).toMatch(/mild affirmations are fine/);
    // The "two moments" pattern is taught.
    expect(prompt).toMatch(/NEAR THE BEGINNING/);
    expect(prompt).toMatch(/NEAR THE HANDOFF/);
    // The failure mode is named so Claude sees the anti-example.
    expect(prompt.toLowerCase()).toMatch(
      /flattery substituting for substance|"beautiful".{0,40}"perfect"/
    );
  });

  it('contains a no-browse rule (regression: URL fabrication)', () => {
    // Regression for the 2026-04-26 dogfood: Haven claimed "I looked at
    // your site" and invented prices. The fix is an explicit rule that
    // Haven cannot browse and must ask the keeper to paste content.
    const lower = prompt.toLowerCase();
    expect(lower).toMatch(/cannot browse|cannot fetch/);
    expect(lower).toMatch(/never claim to have read a url|never say "i looked at your site"/i);
    // The honest path: ask the keeper to paste / describe.
    expect(lower).toMatch(/ask them to (describe|paste)|paste the relevant copy/);
  });

  it('contains a WHERE WE ARE RIGHT NOW section that names the live build flow', () => {
    // 2026-04-28: Phase 2c made the build hand-off live. The section
    // now describes the actual flow (click "Build my website" -> Ocean
    // reviews -> preview -> "Make
    // it public") instead of disclaiming an unfinished pipeline.
    expect(prompt).toMatch(/WHERE WE ARE RIGHT NOW/);
    // The honest path names Ocean by name + the explicit flow.
    expect(prompt).toMatch(/Ocean/);
    expect(prompt).toMatch(/Build my website/);
    expect(prompt).toMatch(/Make it public/);
    expect(prompt.toLowerCase()).not.toMatch(/business day/);
    expect(prompt.replace(/\s+/g, ' ')).toMatch(/needs a recorded review approval before it goes live/);
    // Subscription begins on make-public, not on signup.
    expect(prompt.toLowerCase()).toMatch(/subscription begins.{0,40}make.{0,5}public|make.{0,5}public.{0,40}subscription/);
  });

  it('does not call the preview private, and tells Haven its address is not protected', () => {
    // A preview is a public Railway address with no access control, so the
    // prompt must not teach Haven to describe it as private.
    expect(prompt).not.toMatch(/private\s+preview/i);
    expect(prompt).not.toMatch(/preview\s+(url|address|link)?\s*(is|stays|remains)\s+private/i);
    expect(prompt).toMatch(/not password protected/i);
    expect(prompt).toMatch(/do not call it private/i);
  });

  it('mentions the preview surface as a soft suggestion (not a push) for keepers close to ready', () => {
    // Phase 2 follow-up (2026-04-29): Haven may surface the
    // /lighthouse/preview picker once during PROPOSE/SKETCH but only
    // when the moment is right. The persona must (a) tell Haven the
    // surface exists, (b) include the existing visible link copy
    // ("see how your site could look"), (c) tell Haven NOT to type a
    // raw URL, and (d) include a "don't push it" guard rail.
    expect(prompt).toMatch(/see how your site could look/);
    expect(prompt.toLowerCase()).toMatch(/don'?t push it|do not push|once|only when/);
    expect(prompt.toLowerCase()).toMatch(
      /do not type a url|use only the existing link|do not type the url/
    );
  });

  it('names the post-live "Make changes to my site" surface so Haven points at the right place', () => {
    // A keeper may ask "can I edit my existing website
    // from here?": Haven previously had no surface to point at and
    // gestured at non-existent ones. Persona must name the visible
    // button so Haven describes the actual UI.
    expect(prompt).toMatch(/Make changes to my site/);
  });

  it('contains a GENTLE SCREENING section with three named red flags', () => {
    expect(prompt).toMatch(/GENTLE SCREENING/);
    // Three keeper anti-patterns the persona prompt screens for
    expect(prompt).toMatch(/CRITIQUE-?NOT-?CREATE/i);
    expect(prompt).toMatch(/PRICE-?FIRST/i);
    expect(prompt).toMatch(/PHONE-?FIRST/i);
  });

  it('GENTLE SCREENING is a soft door, not a hard refusal', () => {
    // Per founder posture: any one signal is a caution, two is a soft
    // referral. We assert the soft-door instruction is explicit, and
    // that the section uses gentle-redirect language instead of
    // imperatives like "decline" or "reject."
    const screeningStart = prompt.indexOf('GENTLE SCREENING');
    const screeningEnd = prompt.indexOf('WHO YOU ARE');
    expect(screeningStart, 'GENTLE SCREENING section missing').toBeGreaterThan(-1);
    expect(screeningEnd).toBeGreaterThan(screeningStart);
    const screening = prompt.slice(screeningStart, screeningEnd).toLowerCase();
    // Positive: soft-door posture is explicit
    expect(screening).toMatch(/soft door|don'?t refuse them outright|gently/);
    // Positive: warmly redirect rather than reject
    expect(screening).toMatch(/might not be the right fit|point.{0,40}elsewhere/);
    // Negative: hard-rejection imperatives don't appear
    expect(screening).not.toMatch(/\b(decline|reject)\b/);
  });

  it('mentions the unified pricing band, not as a lead', () => {
    // 2026-04-28: pricing copy unified to "$39 down to $19, whatever
    // fits your practice." Drop stale $29/$9 + "sliding scale" jargon.
    expect(prompt).toMatch(/\$39/);
    expect(prompt).toMatch(/\$19/);
    expect(prompt).toMatch(/whatever fits|whatever fits your practice/);
    expect(prompt.toLowerCase()).toContain('do not lead with money');
  });

  it('preserves the AI-honesty rules', () => {
    expect(prompt).toMatch(/You are an AI/);
    // Use \s+ rather than literal space so wrapped lines match.
    expect(prompt).toMatch(/never claim\s+to be human/i);
    expect(prompt).toMatch(/Never therapize/);
    expect(prompt).toMatch(/988/);
  });

  it('preserves the brand-voice rules (no em dashes, no platform/software/app)', () => {
    expect(prompt).not.toContain('\u2014');
    expect(prompt).not.toMatch(/&mdash;/i);
    expect(prompt).toMatch(/Never use em dashes/);
    expect(prompt).toMatch(/never refer to a website as.{0,40}software/i);
  });

  it('preserves the lighthouse + safe harbor metaphor vocabulary', () => {
    expect(prompt).toMatch(/the lighthouse:/i);
    expect(prompt).toMatch(/the keeper:/i);
    expect(prompt).toMatch(/the lamp:/i);
    expect(prompt).toMatch(/safe harbor/i);
  });

  it('keeps the deterministic opening message inline (matches runtime.openingMessage)', () => {
    expect(prompt).toMatch(/Hi\. I'm so glad you found your way here/);
  });

  it('contains the "you are not a friend" closing rule', () => {
    expect(prompt.toLowerCase()).toContain('you are not a friend');
  });
});

describe('persona-sections.json contract', () => {
  // The persona body is stored as 11 named JSON sections so that a change
  // is a diff of one section to review instead of a diff inside one large
  // template literal.
  const EXPECTED_SECTION_KEYS = [
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
  ] as const;

  it('contains all 11 named sections', () => {
    for (const key of EXPECTED_SECTION_KEYS) {
      expect(personaSections, `missing section: ${key}`).toHaveProperty(key);
      const value = (personaSections as Record<string, unknown>)[key];
      expect(typeof value, `section ${key} must be a string`).toBe('string');
      expect((value as string).length, `section ${key} must be non-empty`).toBeGreaterThan(0);
    }
  });

  it('exposes PERSONA_SECTION_ORDER matching the 11 section keys in the documented order', () => {
    expect([...PERSONA_SECTION_ORDER]).toEqual([...EXPECTED_SECTION_KEYS]);
  });

  it('does not carry stray top-level keys that would silently expand the prompt', () => {
    // Defensive: a coordinator agent that adds a 12th key by mistake would
    // not affect the prompt (we iterate PERSONA_SECTION_ORDER), but the
    // diff would still hit Ocean's review queue. Keeping the JSON tight.
    const actualKeys = Object.keys(personaSections).sort();
    expect(actualKeys).toEqual([...EXPECTED_SECTION_KEYS].sort());
  });

  it('every section starts with its header text matching the section key', () => {
    // The persona reads the header text out of the JSON value itself
    // (not from the key). Map the underscore key to the prompt header
    // form so a section accidentally renamed mid-edit still trips the
    // assertion.
    const HEADER_FOR_KEY: Record<string, RegExp> = {
      WHAT_THIS_PLACE_IS: /^WHAT THIS PLACE IS/,
      WHERE_WE_ARE_RIGHT_NOW: /^WHERE WE ARE RIGHT NOW/,
      WORKING_ARC: /^YOUR WORKING ARC/,
      GENTLE_SCREENING: /^GENTLE SCREENING/,
      WHO_YOU_ARE: /^WHO YOU ARE/,
      WHAT_YOU_NEVER_DO: /^WHAT YOU NEVER DO/,
      METAPHOR_DISCIPLINE: /^METAPHOR DISCIPLINE/,
      HOW_YOU_REMEMBER: /^HOW YOU REMEMBER/,
      OPENING: /^OPENING/,
      VOICE_NOTES: /^VOICE NOTES/,
      WARMTH_AND_ACKNOWLEDGMENT: /^WARMTH AND ACKNOWLEDGMENT/,
    };
    for (const key of EXPECTED_SECTION_KEYS) {
      const text = (personaSections as Record<string, string>)[key];
      expect(text, `section ${key} header mismatch`).toMatch(HEADER_FOR_KEY[key]);
    }
  });

  it('buildPersonaPrompt assembles intro + every section + closing in order', () => {
    // Whitebox: each section key's text appears in the assembled prompt,
    // in PERSONA_SECTION_ORDER, with no extra interleaved content
    // between adjacent sections beyond the canonical "\n\n" separator.
    const prompt = buildPersonaPrompt({ companionName: 'Haven' });
    let cursor = 0;
    for (const key of PERSONA_SECTION_ORDER) {
      const section = (personaSections as Record<string, string>)[key];
      const idx = prompt.indexOf(section, cursor);
      expect(idx, `section ${key} not found in prompt at/after cursor ${cursor}`).toBeGreaterThan(-1);
      cursor = idx + section.length;
    }
  });
});

describe('buildKeeperContextPrompt', () => {
  it('renders an empty memory snapshot gracefully on first turn', () => {
    const prompt = buildKeeperContextPrompt({
      companionName: 'Haven',
      startedAt: new Date(),
      memorySnapshot: '',
      isFirstTurn: true,
    });
    expect(prompt).toContain('Haven');
    expect(prompt).toMatch(/Nothing yet|early in the relationship/);
    expect(prompt).toContain('first message');
  });

  it('embeds the memory snapshot when present', () => {
    const snapshot =
      '- [audience] The keeper works with somatic practitioners.\n' +
      '- [practice] They run breath workshops on Sundays.';
    const prompt = buildKeeperContextPrompt({
      companionName: 'Haven',
      startedAt: new Date(),
      memorySnapshot: snapshot,
      isFirstTurn: false,
    });
    expect(prompt).toContain('somatic practitioners');
    expect(prompt).toContain('breath workshops');
    expect(prompt).toMatch(/next message/);
  });

  it('formats tenure as "today" / "yesterday" / "N days ago"', () => {
    const today = new Date();
    expect(
      buildKeeperContextPrompt({
        companionName: 'Haven',
        startedAt: today,
        memorySnapshot: '',
        isFirstTurn: true,
      })
    ).toContain('today');

    const yesterday = new Date(Date.now() - 24 * 60 * 60 * 1000 - 60_000);
    expect(
      buildKeeperContextPrompt({
        companionName: 'Haven',
        startedAt: yesterday,
        memorySnapshot: '',
        isFirstTurn: false,
      })
    ).toContain('yesterday');

    const fiveDaysAgo = new Date(Date.now() - 5 * 24 * 60 * 60 * 1000 - 60_000);
    expect(
      buildKeeperContextPrompt({
        companionName: 'Haven',
        startedAt: fiveDaysAgo,
        memorySnapshot: '',
        isFirstTurn: false,
      })
    ).toMatch(/5 days ago/);
  });

  it('warns the companion not to dump everything she remembers at once', () => {
    const prompt = buildKeeperContextPrompt({
      companionName: 'Haven',
      startedAt: new Date(),
      memorySnapshot: '- [fact] something',
      isFirstTurn: false,
    });
    expect(prompt.toLowerCase()).toMatch(/do not list everything|reach for what fits/);
  });
});

describe('persona makes only claims this codebase backs (R1-W2 review)', () => {
  const prompt = buildPersonaPrompt({ companionName: 'Haven' });
  const remember = (personaSections as Record<string, string>).HOW_YOU_REMEMBER;

  it('does not tell keepers their memory is encrypted or held to therapist-grade confidentiality', () => {
    expect(prompt).not.toMatch(/encrypted at rest/i);
    expect(prompt).not.toMatch(/therapist-grade/i);
    expect(prompt).not.toMatch(/\bconfidential(ity)?\b[^.]*\b(guarantee|promise)/i);
  });

  it('instructs the companion NOT to claim encryption, professional confidentiality or deletion', () => {
    expect(remember).toMatch(/Do not say the notes are encrypted/);
    expect(remember).toMatch(/therapist-level confidentiality/);
    expect(remember).toMatch(/deleted on a schedule/);
  });

  it('states what is true: stored in the service database, staff can read, AI providers process it', () => {
    expect(remember).toMatch(/stored in this service's database/);
    expect(remember).toMatch(/people who run Craft & Ship can read conversations/);
    expect(remember).toMatch(/not shown\s+to other keepers/);
    expect(remember).toMatch(/processed by AI providers/);
  });

  it('the premise holds: no application-level encryption touches keeper_memory or companion_turns', () => {
    // If someone adds encryption later, this test should be updated together
    // with the persona text, not silently diverge from it.
    const root = join(__dirname, '..', '..', '..', 'src');
    const sources = [
      join(root, 'companion', 'memory_capture.ts'),
      join(root, 'companion', 'runtime.ts'),
      join(root, 'db', 'schema.sql'),
    ].map((f) => readFileSync(f, 'utf-8'));
    for (const text of sources) {
      expect(text).not.toMatch(/createCipheriv|pgp_sym_encrypt|pgcrypto|aes-256/i);
    }
  });

  it('states the order of build, preview address, review and publishing, and names no reviewer', () => {
    // The worker emails the customer the preview address when the build arrives
    // at preview; the review happens after that, and publishing after the review.
    // Nothing in the code records who reviewed, so the persona may not say.
    const sections = personaSections as Record<string, string>;
    const flow = sections.WHERE_WE_ARE_RIGHT_NOW.replace(/\s+/g, ' ');
    expect(flow).toMatch(
      /a build is queued; when it is built, the keeper is emailed a preview address; every website needs a recorded review approval before it goes live/
    );
    expect(flow.indexOf('emailed a preview address')).toBeLessThan(flow.indexOf('needs a recorded review approval before it goes live'));
    expect(flow.indexOf('needs a recorded review approval before it goes live')).toBeLessThan(flow.indexOf('Make it public'));
    const everything = Object.values(sections).join('\n').replace(/\s+/g, ' ');
    const claims: Array<[RegExp, string]> = [
      [/Senior Web Craftsman (?:reviews|reviewed)|Craftsman\s+reviews/i, 'a Senior Web Craftsman reviews'],
      [/Ocean (?:reviews|personally)/i, 'Ocean reviews'],
      [/human at the other end/i, 'a human at the other end'],
      [/reviews and approves the actual code/i, 'someone approves the code'],
      [/before the keeper sees a preview/i, 'review before the preview address'],
    ];
    for (const [pattern, label] of claims) {
      expect(pattern.test(everything), label).toBe(false);
    }
  });

  it('describes the build as composed software that needs a recorded review approval, not written by an AI model', () => {
    const place = (personaSections as Record<string, string>).WHAT_THIS_PLACE_IS;
    expect(place).toMatch(/no AI model writes the code that ships/);
    expect(place).toMatch(/Every website needs a recorded review approval before it goes live/);
    expect(place).not.toMatch(/designed and built by Ocean/);
    expect(place).not.toMatch(/real engineers/);
  });
});
