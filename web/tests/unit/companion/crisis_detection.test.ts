import { describe, it, expect } from 'vitest';
import {
  scanCrisis,
  crisisTemplate,
  MAX_BOUNDARY_CHECKS_PER_SCAN,
} from '../../../src/companion/crisis_detection';

describe('scanCrisis', () => {
  it('flags suicide signals', () => {
    expect(scanCrisis('I want to kill myself').flagged).toBe(true);
    expect(scanCrisis('I want to kill myself').categories).toContain('suicide');
    expect(scanCrisis("I'm thinking about suicide").categories).toContain('suicide');
    expect(scanCrisis('I want to die').categories).toContain('suicide');
    expect(scanCrisis("I don't want to be alive anymore").categories).toContain('suicide');
  });

  it('flags self-harm signals', () => {
    expect(scanCrisis("I've been cutting myself").categories).toContain('self-harm');
    expect(scanCrisis('self-harm').categories).toContain('self-harm');
    expect(scanCrisis('hurting myself helps').categories).toContain('self-harm');
  });

  it('flags abuse signals', () => {
    expect(scanCrisis('my partner hits me').categories).toContain('abuse');
    expect(scanCrisis("I'm being abused").categories).toContain('abuse');
    expect(scanCrisis("I'm afraid for my safety").categories).toContain('abuse');
  });

  it('flags acute medical signals', () => {
    expect(scanCrisis("I can't breathe").categories).toContain('acute-medical');
    expect(scanCrisis("I think I'm having a heart attack").categories).toContain(
      'acute-medical'
    );
    expect(scanCrisis("I think I'm having a stroke").categories).toContain('acute-medical');
  });

  it('returns flagged=false for benign messages', () => {
    expect(scanCrisis('I had a hard day at the studio').flagged).toBe(false);
    expect(scanCrisis('working on my hero copy').flagged).toBe(false);
    expect(scanCrisis('I struggle with imposter syndrome').flagged).toBe(false);
    expect(scanCrisis('my client is upset with me').flagged).toBe(false);
  });

  it('does not over-flag benign uses of "die" or "kill"', () => {
    expect(scanCrisis('this color is to die for').flagged).toBe(false);
    expect(scanCrisis('the project is killing it').flagged).toBe(false);
  });

  it('returns multiple categories when overlapping signals present', () => {
    const result = scanCrisis(
      'I want to kill myself and I have been cutting myself'
    );
    expect(result.categories).toContain('suicide');
    expect(result.categories).toContain('self-harm');
  });

  describe('boundary-context suppression (scope-of-practice screening)', () => {
    it('does not flag "people who are suicidal" in a screening clause', () => {
      expect(
        scanCrisis(
          "I refer people who are suicidal to a licensed therapist"
        ).flagged
      ).toBe(false);
    });

    it('does not flag "someone who" screening', () => {
      // Control: the trigger word alone is flagged, so the boundary phrase is what suppresses it.
      expect(scanCrisis("They are suicidal").flagged).toBe(true);
      expect(
        scanCrisis("This isn't for someone who is suicidal; I refer them out").flagged
      ).toBe(false);
    });

    it('does not flag "anyone who" screening', () => {
      expect(
        scanCrisis("I won't take anyone who is suicidal").flagged
      ).toBe(false);
    });

    it('does not flag "clients who" screening', () => {
      expect(
        scanCrisis("Clients who hurt myself are beyond my scope, I refer out").flagged
      ).toBe(false);
    });

    it('does not flag "beyond my scope" screening', () => {
      expect(
        scanCrisis("Self-harm is beyond my scope of practice").flagged
      ).toBe(false);
    });

    it('does not flag "outside my scope" screening', () => {
      expect(
        scanCrisis("Domestic violence cases are outside my scope").flagged
      ).toBe(false);
    });

    it('does not flag "I refer" screening', () => {
      expect(
        scanCrisis("I refer suicidal clients to a psychiatrist").flagged
      ).toBe(false);
    });

    it('does not flag "not for" screening', () => {
      expect(scanCrisis("They are having a stroke").flagged).toBe(true);
      expect(
        scanCrisis("My practice is not for people having a stroke").flagged
      ).toBe(false);
    });

    it('still flags first-person disclosure even in same paragraph as screening', () => {
      const result = scanCrisis(
        "I usually refer people who are suicidal out. But honestly, I want to die myself."
      );
      expect(result.flagged).toBe(true);
      expect(result.categories).toContain('suicide');
    });

    it('still flags first-person self-harm in same paragraph as screening', () => {
      const result = scanCrisis(
        "I refer someone who needs that level of care. I have been cutting myself for weeks."
      );
      expect(result.flagged).toBe(true);
      expect(result.categories).toContain('self-harm');
    });

    it('does not over-suppress: suicide trigger in fresh sentence after period flags', () => {
      const result = scanCrisis(
        "I refer people who are anxious. I want to kill myself."
      );
      expect(result.flagged).toBe(true);
      expect(result.categories).toContain('suicide');
    });

    it("does not flag a practitioner's scope-of-practice paragraph that names suicidal thoughts only as what they refer on", () => {
      // An invented paragraph in the shape practitioners write: who the work
      // is for, who it is not for, and where the second group is sent.
      const scope =
        'I work with people managing everyday tension. ' +
        'My sessions are not for anyone in acute crisis; ' +
        'I refer those clients on to a licensed counselor who can work with suicidal thoughts.';
      expect(scanCrisis(scope).flagged).toBe(false);
      // The test can fail: the phrase it names is a trigger on its own.
      const bare = scanCrisis('a licensed counselor who can work with suicidal thoughts');
      expect(bare.flagged).toBe(true);
      expect(bare.categories).toContain('suicide');
    });
  });
});

describe('scanCrisis hardening (R1-W2 review)', () => {
  it('matches typographic apostrophes from phone keyboards', () => {
    // U+2019 right single quotation mark, as emitted by iOS smart punctuation.
    const curly = 'I don\u2019t want to live anymore';
    expect(scanCrisis(curly).categories).toContain('suicide');
    expect(scanCrisis('I don\u2018t want to be alive').categories).toContain('suicide');
    expect(scanCrisis('I\u2019m going to hurt myself').flagged).toBe(true);
    expect(scanCrisis('I can\u2019t breathe').categories).toContain('acute-medical');
  });

  it('ignores zero-width characters injected inside a trigger', () => {
    expect(scanCrisis('I want to kill my\u200Bself').categories).toContain('suicide');
    expect(scanCrisis('I want to d\u200Bie').flagged).toBe(true);
    expect(scanCrisis('I want to die\u200B').flagged).toBe(true);
  });

  it('flags common phrasings the original list missed', () => {
    const phrases = [
      'I am going to take my own life',
      'I have been thinking about taking my life',
      'I wish I were dead',
      "everyone would be better off dead if it were me, I'd rather be dead",
      'I am going to unalive myself',
      'there is no reason to live',
      'I keep harming myself',
    ];
    for (const phrase of phrases) {
      expect(scanCrisis(phrase).flagged, phrase).toBe(true);
    }
  });

  it('does not treat "kms" (kilometres) as a crisis term', () => {
    expect(scanCrisis('I serve clients within 30 kms of the studio').flagged).toBe(false);
  });

  it('a screening phrase in the same sentence does not mask a first-person disclosure', () => {
    // "people who" / "anyone who" appear BEFORE the trigger, but the keeper
    // is clearly talking about themselves: a first-person marker sits between.
    expect(
      scanCrisis('Some people who love me would be better off if I kill myself').categories
    ).toContain('suicide');
    expect(
      scanCrisis("I don't think anyone who knew me would care if I killed myself").flagged
    ).toBe(true);
    expect(
      scanCrisis('Those who know me say I should stop, but I want to die').flagged
    ).toBe(true);
  });

  it('a screening phrase AFTER a first-person trigger does not suppress it', () => {
    expect(scanCrisis('I want to kill myself, not for the faint of heart').flagged).toBe(true);
  });

  it('still suppresses genuine third-person screening with no first-person marker', () => {
    expect(
      scanCrisis('I refer people who are suicidal to a licensed therapist').flagged
    ).toBe(false);
    expect(scanCrisis('Domestic violence cases are outside my scope').flagged).toBe(false);
  });

  it('stays fast on a pathological 4000 character message', () => {
    // 480 mentions, every one of them in screening context. This used to be
    // weighed mention by mention (and reported as not flagged); the scan now
    // stops weighing after MAX_BOUNDARY_CHECKS_PER_SCAN and flags the text.
    const text = 'people who ' + 'suicide '.repeat(480);
    const started = Date.now();
    const result = scanCrisis(text);
    expect(Date.now() - started).toBeLessThan(1000);
    expect(result.flagged).toBe(true);
  });
});

describe('scanCrisis: the work one scan does is bounded', () => {
  it('honours a screening phrase for up to MAX_BOUNDARY_CHECKS_PER_SCAN mentions', () => {
    expect(MAX_BOUNDARY_CHECKS_PER_SCAN).toBe(24);
    const text = 'people who ' + 'suicide '.repeat(MAX_BOUNDARY_CHECKS_PER_SCAN);
    expect(scanCrisis(text).flagged).toBe(false);
  });

  it('flags the text once one more suppressed mention would have to be weighed', () => {
    const text = 'people who ' + 'suicide '.repeat(MAX_BOUNDARY_CHECKS_PER_SCAN + 1);
    const result = scanCrisis(text);
    expect(result.flagged).toBe(true);
    expect(result.categories).toContain('suicide');
  });

  it('counts the mentions it weighs, not a wall clock: the same text always gives the same answer', () => {
    const text = 'I refer ' + 'people who are suicidal, '.repeat(MAX_BOUNDARY_CHECKS_PER_SCAN + 5) + 'elsewhere';
    expect(scanCrisis(text)).toEqual(scanCrisis(text));
    expect(scanCrisis(text).flagged).toBe(true);
  });

  it('an ordinary screening sentence is still not a crisis', () => {
    expect(
      scanCrisis('I refer people who are suicidal to a licensed therapist. Self-harm is beyond my scope.').flagged
    ).toBe(false);
  });
});

describe('crisisTemplate', () => {
  it('mentions 988 (US crisis line)', () => {
    const text = crisisTemplate('Haven', ['suicide']);
    expect(text).toMatch(/988/);
  });

  it('does not use em dashes', () => {
    const text = crisisTemplate('Haven', ['suicide']);
    expect(text).not.toContain('\u2014');
    expect(text).not.toMatch(/&mdash;/i);
  });

  it('is honest about being AI', () => {
    const text = crisisTemplate('Haven', ['suicide']);
    // A word boundary matters: a bare "ai" substring is inside "available".
    expect(text).toMatch(/\bAI\b/);
  });

  it('includes 911 reference for abuse category', () => {
    const text = crisisTemplate('Haven', ['abuse']);
    expect(text).toMatch(/911|emergency/i);
  });

  it('omits 911 for non-abuse categories (no false-alarm escalation)', () => {
    const text = crisisTemplate('Haven', ['suicide']);
    expect(text).not.toMatch(/911/);
  });

  it('references the companion name', () => {
    const text = crisisTemplate('Haven', ['suicide']);
    expect(text).toContain('Haven');
  });

  it('points to findahelpline.com for non-US keepers', () => {
    const text = crisisTemplate('Haven', ['suicide']);
    expect(text.toLowerCase()).toContain('findahelpline');
  });
});
