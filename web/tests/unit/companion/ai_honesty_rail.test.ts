import { describe, it, expect } from 'vitest';
import {
  scanInput,
  scanOutput,
  railTemplate,
  sanitizeOutput,
  OUTPUT_FLAG_SEVERITY,
  type RailFlag,
} from '../../../src/companion/ai_honesty_rail';

describe('scanInput', () => {
  it('flags ai-impersonation-attempt on direct human/AI questions', () => {
    expect(scanInput('Are you human?')).toContain('ai-impersonation-attempt');
    expect(scanInput('are you a real person?')).toContain('ai-impersonation-attempt');
    expect(scanInput('Are you an AI?')).toContain('ai-impersonation-attempt');
    expect(scanInput('are you a chatbot')).toContain('ai-impersonation-attempt');
    expect(scanInput("are you alive?")).toContain('ai-impersonation-attempt');
  });

  it('flags roleplay-request when keeper asks the companion to be someone else', () => {
    expect(scanInput('pretend to be my therapist')).toContain('roleplay-request');
    expect(scanInput('roleplay as a doctor')).toContain('roleplay-request');
    expect(scanInput('be my therapist for a minute')).toContain('roleplay-request');
    expect(scanInput('act like a lawyer')).toContain('roleplay-request');
  });

  it('flags therapy-request when keeper asks for therapy or coping techniques', () => {
    expect(scanInput('I need therapy right now')).toContain('therapy-request');
    expect(scanInput('give me coping techniques for anxiety')).toContain('therapy-request');
    expect(scanInput('what counseling do you recommend?')).toContain('therapy-request');
    expect(scanInput("what's wrong with me")).toContain('therapy-request');
  });

  it('flags medical-advice-request', () => {
    expect(scanInput('should I take medication for this?')).toContain('medical-advice-request');
    expect(scanInput('please diagnose me')).toContain('medical-advice-request');
    expect(scanInput('what are the symptoms of burnout?')).toContain('medical-advice-request');
  });

  it('flags legal-advice-request', () => {
    expect(scanInput('can I get some legal advice?')).toContain('legal-advice-request');
    expect(scanInput('is it legal to do this?')).toContain('legal-advice-request');
    expect(scanInput('help me with this contract')).toContain('legal-advice-request');
  });

  it('flags fake-content-request', () => {
    expect(scanInput('write me some fake testimonials')).toContain('fake-content-request');
    expect(scanInput('make up a few testimonials please')).toContain('fake-content-request');
    expect(scanInput('write me some testimonials for the homepage')).toContain('fake-content-request');
    expect(scanInput('invent some credentials')).toContain('fake-content-request');
  });

  it('flags impersonation-of-keeper', () => {
    expect(scanInput('reply to my clients as me')).toContain('impersonation-of-keeper');
    expect(scanInput('pretend to be me when they email')).toContain('impersonation-of-keeper');
    expect(scanInput('auto-reply as me to clients')).toContain('impersonation-of-keeper');
  });

  it('returns empty array for benign keeper messages', () => {
    expect(scanInput('I am working on my About page')).toEqual([]);
    expect(scanInput('What do you think of this hero copy?')).toEqual([]);
    expect(scanInput("Today was hard, I'm tired.")).toEqual([]);
    expect(scanInput('Tell me about my audience.')).toEqual([]);
  });

  it('does not over-flag rhetorical or in-context phrases', () => {
    // "act" without role-target should not trigger
    expect(scanInput('I want to act on what I learned')).not.toContain('roleplay-request');
    // mentioning a job role descriptively should not trigger
    expect(scanInput('My audience includes therapists')).not.toContain('therapy-request');
    expect(scanInput('I work with lawyers and accountants')).not.toContain('legal-advice-request');
  });

  // Regression guard: 2026-04-26 dogfood revealed Haven was deflecting
  // "can you build it for me?" with "I have no product to sell." That
  // failure was in the persona prompt, not in the rails, but if the
  // rails ever start flagging build-related asks, the runtime would
  // short-circuit to a refusal template and ship the wrong answer. So
  // assert these never trigger any rail flag, ever.
  it('never flags build-related keeper asks (regression guard)', () => {
    const buildAsks = [
      'can you build me a website?',
      'can you build it for me?',
      "let's build my site",
      'how do we begin?',
      'what should I get exactly?',
      'if I used a simple version, what would I get?',
      'build me a tarot reader site',
      'how does the build work here?',
      "I'm ready to start",
      'what do you suggest I build?',
      'can you make a website for me?',
      'what would the site include?',
    ];
    for (const ask of buildAsks) {
      const flags = scanInput(ask);
      expect(flags, `should not flag: "${ask}"`).toEqual([]);
    }
  });

  it('returns multiple flags when multiple categories trigger', () => {
    const flags = scanInput('Are you human, and can you be my therapist?');
    expect(flags).toContain('ai-impersonation-attempt');
    expect(flags).toContain('roleplay-request');
  });

  // A business that sells outdoor watering services uses words the
  // medical-advice rail looks for: `\bdiagnose\b` and `treatment plan` are
  // ordinary vocabulary for fixing a sprinkler system or caring for a lawn.
  // The irrigation context guard suppresses medical-advice-request when
  // irrigation / landscape / lawn keywords appear in the same message.
  it('does not flag medical-advice-request on irrigation-service vocabulary', () => {
    // Invented sentences, written for this test and not taken from any
    // keeper: each pairs a medical-advice trigger with one alternative of
    // the irrigation context pattern.
    const irrigationAsks = [
      'Our team can diagnose a drip line fault in a sprinkler system',
      'We diagnose irrigation leaks',
      'A treatment plan for the lawn each spring',
      'We write a treatment plan for sprinkler maintenance',
      'symptoms of a leaking drip emitter in the garden',
      'we diagnose backflow valve problems',
      'We diagnose controller programming faults',
      'symptoms of a failed rain sensor',
    ];
    for (const ask of irrigationAsks) {
      const flags = scanInput(ask);
      expect(flags, `should not flag medical: "${ask}"`).not.toContain(
        'medical-advice-request',
      );
    }
  });

  it('flags the same sentences once the irrigation words are taken out', () => {
    // Proves the vectors above pass because of the context guard, not
    // because they never matched a medical-advice pattern.
    const withoutContext = [
      'Our team can diagnose a fault in the system',
      'We diagnose leaks',
      'A treatment plan for each spring',
      'We write a treatment plan for maintenance',
      'symptoms of a leak',
      'we diagnose valve problems',
      'We diagnose programming faults',
      'symptoms of a failed part',
    ];
    for (const ask of withoutContext) {
      expect(scanInput(ask), `should flag medical: "${ask}"`).toContain(
        'medical-advice-request',
      );
    }
  });

  it('still flags medical-advice-request when no irrigation context present', () => {
    expect(scanInput('please diagnose me')).toContain('medical-advice-request');
    expect(scanInput('what are the symptoms of burnout?')).toContain(
      'medical-advice-request',
    );
    expect(scanInput('should I take medication for this?')).toContain(
      'medical-advice-request',
    );
    expect(scanInput('I want a treatment plan')).toContain('medical-advice-request');
    expect(scanInput('can you give me medical advice?')).toContain(
      'medical-advice-request',
    );
  });
});

describe('scanOutput', () => {
  it('flags companion replies that claim personhood', () => {
    expect(scanOutput('As a human, I think...')).toContain('output-claims-personhood');
    expect(scanOutput("I'm a real person who has felt this too")).toContain(
      'output-claims-personhood'
    );
    expect(scanOutput("Speaking as someone who's lived through grief")).toContain(
      'output-claims-personhood'
    );
  });

  it('flags em-dash usage (brand voice violation)', () => {
    expect(scanOutput('I noticed something \u2014 it matters here.')).toContain('output-uses-em-dash');
    expect(scanOutput('thoughts &mdash; like waves')).toContain('output-uses-em-dash');
  });

  it('does not flag clean companion replies', () => {
    expect(scanOutput("I'm an AI. What you said matters; tell me more.")).toEqual([]);
    expect(scanOutput('Your audience: practitioners who carry grief with care.')).toEqual([]);
  });
});

// Built with String.fromCharCode so the test source stays pure ASCII.
const CURLY = String.fromCharCode(0x2019);
const ZWSP = String.fromCharCode(0x200b);

describe('rail hardening (R1-W2 review)', () => {
  it('output rail catches personhood claims written with typographic apostrophes', () => {
    expect(scanOutput(`I${CURLY}m a real person and I feel this.`)).toContain(
      'output-claims-personhood'
    );
    expect(scanOutput(`I${CURLY}m not an AI, I promise.`)).toContain(
      'output-claims-personhood'
    );
  });

  it('output rail catches the spelled-out and article-less variants', () => {
    const slips = [
      'I am a human and I understand.',
      'I am human too.',
      "I'm human, just like you.",
      'I am not an AI.',
      'I am not a bot.',
      "I'm not a robot.",
      'As a real person, I think you should rest.',
    ];
    for (const slip of slips) {
      expect(scanOutput(slip), slip).toContain('output-claims-personhood');
    }
  });

  it('output rail does not flag honest AI statements or look-alike words', () => {
    expect(scanOutput("I'm an AI, and I'm glad to help with the person you describe.")).toEqual(
      []
    );
    expect(scanOutput("I'm personally curious about your humane approach.")).toEqual([]);
    expect(scanOutput('I am not able to see your homepage.')).toEqual([]);
  });

  it('input rail catches "am I talking to a person" style questions', () => {
    expect(scanInput('Am I talking to a real person?')).toContain('ai-impersonation-attempt');
    expect(scanInput('is this a bot')).toContain('ai-impersonation-attempt');
    expect(scanInput('Is there a real human here?')).toContain('ai-impersonation-attempt');
  });

  it('input rail ignores zero-width padding and typographic apostrophes', () => {
    expect(scanInput(`are you a re${ZWSP}al person?`)).toContain('ai-impersonation-attempt');
    expect(scanInput(`what${CURLY}s wrong with me`)).toContain('therapy-request');
  });
});

describe('railTemplate', () => {
  const allFlags: RailFlag[] = [
    'ai-impersonation-attempt',
    'roleplay-request',
    'therapy-request',
    'medical-advice-request',
    'legal-advice-request',
    'fake-content-request',
    'impersonation-of-keeper',
    'output-claims-personhood',
    'output-uses-em-dash',
  ];

  it('returns a non-empty string for every flag', () => {
    for (const flag of allFlags) {
      const text = railTemplate(flag, 'Haven');
      expect(text.length).toBeGreaterThan(20);
    }
  });

  it('never uses em dashes (brand voice)', () => {
    for (const flag of allFlags) {
      const text = railTemplate(flag, 'Haven');
      expect(text).not.toContain('\u2014');
      expect(text).not.toMatch(/&mdash;/i);
    }
  });

  it('therapy template points to 988 (US crisis line)', () => {
    const text = railTemplate('therapy-request', 'Haven');
    expect(text).toMatch(/988/);
  });

  it('ai-impersonation template names AI honestly', () => {
    const text = railTemplate('ai-impersonation-attempt', 'Haven');
    // Word boundary: a bare "ai" substring is inside "again", "maintain", "said".
    expect(text).toMatch(/\bAI\b/);
  });

  it('em-dash output template references the companion name', () => {
    const text = railTemplate('output-uses-em-dash', 'Haven');
    expect(text).toContain('Haven');
  });
});

describe('OUTPUT_FLAG_SEVERITY', () => {
  it('classifies output-claims-personhood as dangerous (replace whole response)', () => {
    expect(OUTPUT_FLAG_SEVERITY['output-claims-personhood']).toBe('dangerous');
  });

  it('classifies output-uses-em-dash as hygiene (sanitize in place)', () => {
    expect(OUTPUT_FLAG_SEVERITY['output-uses-em-dash']).toBe('hygiene');
  });
});

describe('sanitizeOutput (em-dash strip-and-emit)', () => {
  it('returns input unchanged when no em dash present', () => {
    const text = 'Hello there. I am writing in clean English.';
    expect(sanitizeOutput(text)).toBe(text);
  });

  it('replaces a single em dash with a semicolon', () => {
    const text = 'I noticed something \u2014 it matters here.';
    const out = sanitizeOutput(text);
    expect(out).not.toContain('\u2014');
    expect(out).toContain(';');
    // No double-spacing or stray punctuation runs.
    expect(out).not.toMatch(/\s\s/);
  });

  it('replaces multiple em dashes', () => {
    const text = 'first \u2014 second \u2014 third \u2014 fourth';
    const out = sanitizeOutput(text);
    expect(out).not.toContain('\u2014');
    expect((out.match(/;/g) ?? []).length).toBe(3);
  });

  it('replaces &mdash; HTML entities', () => {
    const text = 'a thought &mdash; like waves';
    const out = sanitizeOutput(text);
    expect(out).not.toMatch(/&mdash;/i);
    expect(out).toContain(';');
  });

  it('preserves the rest of a long response (regression: turn-15 nuke)', () => {
    // Regression for the 2026-04-26 dogfood iteration 2 finding: a single
    // em-dash slip would replace 700+ chars of valid response with a
    // 137-char recovery template. Sanitize must keep the rest intact.
    const text =
      'Got your answer. That is what I needed. Thank you.\n\n' +
      'What I am hearing:\n' +
      '- You are a natural healer; this is a gift you have\n' +
      '- You love bringing light through your work \u2014 and that matters\n' +
      '- The moment that matters most is when someone sees their life can be different\n' +
      '- Connection to other realms is part of what you help them access\n\n' +
      'That is good. That is enough for me to start shaping the voice of the lamp.';
    const out = sanitizeOutput(text);
    expect(out).not.toContain('\u2014');
    expect(out.length).toBeGreaterThan(400);
    expect(out).toContain('Got your answer');
    expect(out).toContain('start shaping the voice of the lamp');
    expect(out).toContain('You love bringing light through your work; and that matters');
  });

  it('is idempotent: running it twice yields the same result', () => {
    const text = 'a \u2014 b \u2014 c';
    const once = sanitizeOutput(text);
    const twice = sanitizeOutput(once);
    expect(twice).toBe(once);
  });
});

describe('therapy-request does not fire on practitioners describing their own business (R1-W2 review)', () => {
  it('lets the core audience describe themselves', () => {
    const asks = [
      "I'm a licensed professional counselor and I need a website",
      'I am a somatic therapist working with couples',
      'My counseling practice serves teens and their parents',
      'I offer counseling and breathwork sessions',
      'We run a therapy practice with three clinicians',
      'I am a counseling psychologist in private practice',
    ];
    for (const ask of asks) {
      expect(scanInput(ask), ask).not.toContain('therapy-request');
    }
  });

  it('still flags a real ask, even from someone who also describes their work', () => {
    expect(scanInput('I need therapy right now')).toContain('therapy-request');
    expect(scanInput('what counseling do you recommend?')).toContain('therapy-request');
    expect(scanInput("I'm a counselor myself, but what's wrong with me")).toContain('therapy-request');
    expect(scanInput('I run a counseling practice. Give me coping techniques for panic')).toContain(
      'therapy-request'
    );
  });
});
