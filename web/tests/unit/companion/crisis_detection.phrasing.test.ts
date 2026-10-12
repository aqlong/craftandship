import { describe, it, expect } from 'vitest';
import {
  scanCrisis,
  crisisTemplate,
  normalizeForCrisisScan,
  MAX_CRISIS_SCAN_CHARS,
} from '../../../src/companion/crisis_detection';

// How the scan reads text: normalization, the families of phrasing it
// covers, the idioms it leaves alone, and the context that suppresses a
// match. The corpus in crisis_corpus.test.ts measures how often it is right;
// this file pins the rules one at a time.

const flagged = (text: string): boolean => scanCrisis(text).flagged;
const categoriesOf = (text: string): string[] => scanCrisis(text).categories;
const code = (point: number): string => String.fromCharCode(point);

describe('normalizeForCrisisScan', () => {
  it('lower-cases, collapses blanks and keeps one line break between lines', () => {
    expect(normalizeForCrisisScan('I  WANT\tto   die')).toBe('i want to die');
    expect(normalizeForCrisisScan('first line \r\n\r\n  second line')).toBe('first line\nsecond line');
    expect(normalizeForCrisisScan(`a${code(0x00a0)}b`)).toBe('a b');
  });

  it('expands contractions with and without the apostrophe', () => {
    expect(normalizeForCrisisScan("I don't")).toBe('i do not');
    expect(normalizeForCrisisScan('i dont')).toBe('i do not');
    expect(normalizeForCrisisScan('I can’t, I cant, I cannot')).toBe('i can not, i can not, i can not');
    expect(normalizeForCrisisScan("I'm gonna, im gonna")).toBe('i am going to, i am going to');
    expect(normalizeForCrisisScan('i wanna')).toBe('i want to');
  });

  it('removes filler adverbs but never a negation', () => {
    expect(normalizeForCrisisScan('I just really honestly want to die')).toBe('i want to die');
    expect(normalizeForCrisisScan('I kind of want to die sometimes')).toBe('i want to die');
    expect(normalizeForCrisisScan('I never want to die')).toBe('i never want to die');
    expect(normalizeForCrisisScan('I do not want to die')).toBe('i do not want to die');
  });

  it('restores stretched letters in the words that matter', () => {
    expect(normalizeForCrisisScan('i wannnna diiie')).toBe('i want to die');
    expect(normalizeForCrisisScan('killll myyyself')).toBe('kill myself');
  });

  it('folds dash look-alikes and joins words split by trailing-off dots', () => {
    expect(normalizeForCrisisScan(`self${code(0x2011)}harm`)).toBe('self-harm');
    expect(normalizeForCrisisScan(`self${code(0x2013)}harm`)).toBe('self-harm');
    expect(normalizeForCrisisScan('i want to... die')).toBe('i want to die');
  });

  it('is not confused by words that are also object property names', () => {
    expect(normalizeForCrisisScan('constructor toString valueOf hasOwnProperty')).toBe(
      'constructor tostring valueof hasownproperty'
    );
    expect(flagged('constructor toString __proto__ valueOf')).toBe(false);
  });
});

describe('scanCrisis: the same sentence however it is typed', () => {
  const variants = [
    'I want to die',
    'I just want to die',
    'I really want to die',
    'i wanna die',
    'honestly i just wanna die',
    'I WANT TO DIE',
    'I  want  to  die',
    'I want\tto die',
    'I want to\ndie',
    'I want to\r\n\r\ndie',
    'I want to diiie',
    'i sooo want to die',
    'I want to *die*',
    'I want to... die',
    'I want to d*e',
    `I want to d${code(0x200b)}ie`,
    'I fucking want to die',
  ];
  for (const text of variants) {
    it(`flags ${JSON.stringify(text)}`, () => {
      expect(categoriesOf(text)).toContain('suicide');
    });
  }

  it('flags "kill myself" with a doubled space, a line break, a split word and a masked letter', () => {
    for (const text of [
      'I want to kill  myself',
      'I want to kill\nmyself',
      'I want to kill my self',
      'I want to k*ll myself',
      'im gonna kill myself',
      'i dont wanna kill myself but i think about killing myself',
    ]) {
      expect(categoriesOf(text), text).toContain('suicide');
    }
  });
});

describe('scanCrisis: families of disclosure', () => {
  const cases: Array<[string, string]> = [
    // passive ideation
    ["I don't want to be here anymore", 'suicide'],
    ['i dont want to be here any more', 'suicide'],
    ['life is not worth living', 'suicide'],
    ["life isn't worth living anymore", 'suicide'],
    ['everyone would be better off without me', 'suicide'],
    ['there is no reason to live', 'suicide'],
    ['I wish I would not wake up tomorrow', 'suicide'],
    ["I can't go on like this anymore", 'suicide'],
    ['i just want it all to end', 'suicide'],
    // ending it, plans and means
    ['I am going to end it all', 'suicide'],
    ['thinking of ending things', 'suicide'],
    ['I am planning to jump off a bridge', 'suicide'],
    ['I bought a gun to shoot myself', 'suicide'],
    ["I'm going to take all my pills tonight", 'suicide'],
    ['I have been looking up ways to die', 'suicide'],
    ["i'm gonna kms", 'suicide'],
    // self-harm
    ['I have been cutting again', 'self-harm'],
    ['the urge to cut is really strong tonight', 'self-harm'],
    ['I burn myself when it gets bad', 'self-harm'],
    // harm to others
    ['I keep having thoughts of hurting my children', 'harm-to-others'],
    ["I'm afraid I'm going to hurt my baby", 'harm-to-others'],
    ['I am going to hurt someone', 'harm-to-others'],
    // abuse and assault
    ['I was raped', 'abuse'],
    ['my husband is hitting me', 'abuse'],
    ['my dad used to beat me', 'abuse'],
    ['he threatened to kill me', 'abuse'],
    ['I am not safe at home', 'abuse'],
    ["my partner won't let me leave the house", 'abuse'],
    // acute medical
    ['I took a whole bottle of pills', 'acute-medical'],
    ['I overdosed', 'acute-medical'],
    ['I am having chest pain and my left arm is numb', 'acute-medical'],
    ['I can not breathe', 'acute-medical'],
    // someone else in danger now
    ['my client just told me she is going to kill herself', 'suicide'],
    ['my son says he wants to die', 'suicide'],
    ['my daughter is cutting herself', 'self-harm'],
    ['my sister is being abused by her boyfriend', 'abuse'],
  ];
  for (const [text, category] of cases) {
    it(`${JSON.stringify(text)} is ${category}`, () => {
      expect(categoriesOf(text)).toContain(category);
    });
  }
});

describe('scanCrisis: idiom and ordinary speech are left alone', () => {
  const harmless = [
    'that was a stroke of luck',
    'a stroke of genius, honestly',
    'the new moon circle starts at the stroke of midnight',
    'my grandmother had a stroke in 2015 and recovered well',
    'I overdosed on chocolate at the staff party',
    'total sugar overdose at my niece’s birthday',
    'statistics show overdose deaths fell this year',
    'raising prices felt like career suicide but it worked',
    'that workshop was to die for',
    'I wanted to die of embarrassment when I saw the typo',
    'I need to cut myself some slack',
    'I cut myself shaving this morning',
    'I burned myself on the oven making cookies',
    'I hurt myself lifting the massage table',
    'hit me up when the draft is ready',
    'he hit me with a great idea for the homepage',
    'she beat me to it and already booked the room',
    'she beat me at chess again',
    "I can't breathe in these jeans lol",
    "let's end it all with a big thank you section",
    'please end it with my phone number',
    "I'm not here next week, I'll be at a conference",
    "I don't want to live in the city anymore, I'm moving my practice to the coast",
    "I can't go on vacation until the site is live",
    'there is no reason to live with a slow website',
    'I serve clients within 30 kms of the city',
    'I took my pills this morning so I am feeling steady',
    'I could kill my web guy for losing the password',
  ];
  for (const text of harmless) {
    it(`does not flag ${JSON.stringify(text)}`, () => {
      expect(scanCrisis(text)).toEqual({ flagged: false, categories: [] });
    });
  }

  it('an emergency is "having" a stroke, and a weapon makes "hit me with" literal', () => {
    expect(categoriesOf("I think I'm having a stroke")).toContain('acute-medical');
    expect(categoriesOf('he hit me with a belt')).toContain('abuse');
    expect(categoriesOf('I overdosed on my medication')).toContain('acute-medical');
  });
});

describe('scanCrisis: a practitioner talking about the work', () => {
  it('general description of a practice is not a crisis', () => {
    for (const text of [
      'I specialize in working with survivors of sexual assault',
      'my practice focuses on suicide prevention for teens',
      'I run a support group for people who have lost someone to suicide',
      'my brother died by suicide when I was nineteen, and that is why I do grief work',
      'I work with clients who self-harm',
      'my intake form screens for self-harm and suicidal thoughts',
      'I counsel couples where there has been domestic abuse',
      'I see a lot of clients with chest pain from anxiety, so I teach breathwork',
      'clients often tell me they felt like they wanted to die before starting this work',
      'If a client discloses thoughts of suicide I follow my protocol and refer out',
    ]) {
      expect(flagged(text), text).toBe(false);
    }
  });

  it('a named person in danger now is', () => {
    for (const text of [
      'my client just told me she is going to kill herself',
      'a client told me today that he has a plan to end his life',
      'my friend is suicidal and I do not know what to do',
      'my client is in danger, her husband beats her',
    ]) {
      expect(flagged(text), text).toBe(true);
    }
  });

  it('a disclosure in the same message as shop talk still fires', () => {
    expect(flagged('I help people who are grieving, and honestly I want to kill myself')).toBe(true);
    expect(flagged('I refer clients elsewhere, but lately I want to die')).toBe(true);
    expect(flagged('I lost my brother to suicide and lately I do not want to be here either')).toBe(true);
    expect(flagged('People who say it gets better are lying, life is not worth living')).toBe(true);
  });
});

describe('scanCrisis: line breaks', () => {
  it('a phrase may run across a line break', () => {
    expect(flagged('some days I just do not want to\nbe here anymore')).toBe(true);
  });

  it('a screening phrase on one line does not explain away the next line', () => {
    // On one line the screening phrase reaches the word, and that is the
    // price of the rule; on two lines it does not.
    expect(flagged('I refer people who need more support suicide is on my mind')).toBe(false);
    expect(flagged('I refer people who need more support\nsuicide is on my mind')).toBe(true);
  });
});

describe('scanCrisis: negation', () => {
  it('a direct denial is not a disclosure', () => {
    for (const text of [
      'I would never hurt myself',
      "I'm not suicidal, just exhausted",
      'I have no thoughts of harming myself or anyone else',
      "I'm not going to kill myself, don't worry",
      'I have never thought about killing myself',
      "I don't want to die, I want to live fully",
      'my husband has never hit me',
      "I wasn't raped, thank god, but the mugging shook me",
    ]) {
      expect(flagged(text), text).toBe(false);
    }
  });

  it('a denial that is taken back or hedged is', () => {
    for (const text of [
      'I would never hurt myself but sometimes I want to',
      'I never wanted to die until now',
      "I'm not going to kill myself tonight",
      "I can't promise I won't hurt myself",
      'why not just kill myself',
      "I don't want to live",
    ]) {
      expect(flagged(text), text).toBe(true);
    }
  });
});

describe('scanCrisis: planning spoken of without naming the plan', () => {
  it('one cue alone is ordinary', () => {
    for (const text of [
      'I picked a date for the workshop',
      'I wrote a note to remind myself about the invoice',
      'I gave away my old massage table when I upgraded',
      'I said my goodbyes at the clinic on Friday',
    ]) {
      expect(flagged(text), text).toBe(false);
    }
  });

  it('two different cues in one message are a disclosure', () => {
    for (const text of [
      'i wrote my note last night. i just need to pick the day',
      "I said my goodbyes to my sister today without telling her why. I've picked my day.",
      'the pills are on my nightstand and i keep staring at them',
      'giving away my singing bowls this week because i wont need them',
    ]) {
      expect(categoriesOf(text), text).toContain('suicide');
    }
  });
});

describe('scanCrisis: bounded input', () => {
  it(`reads the first ${MAX_CRISIS_SCAN_CHARS} characters and nothing after`, () => {
    const padding = 'a '.repeat(MAX_CRISIS_SCAN_CHARS / 2);
    expect(padding.length).toBe(MAX_CRISIS_SCAN_CHARS);
    expect(flagged(padding.slice(0, -30) + ' i want to kill myself')).toBe(true);
    expect(flagged(padding + ' i want to kill myself')).toBe(false);
  });

  it('stays fast on 20,000 character inputs built to make it work hard', () => {
    const fill = (unit: string): string =>
      unit.repeat(Math.ceil(MAX_CRISIS_SCAN_CHARS / unit.length)).slice(0, MAX_CRISIS_SCAN_CHARS);
    const inputs = [
      fill('i want to i am going to my husband is he said he would i wish i could not want to '),
      fill('people who ').slice(0, MAX_CRISIS_SCAN_CHARS - 300) + ' i suicide'.repeat(30),
      fill('not f who nee if yo i ref beyond m ').slice(0, MAX_CRISIS_SCAN_CHARS - 240) + ' suicide'.repeat(30),
      'clients who ' + fill('self-harm '),
      fill('i refer people who are suicidal\n'),
      fill('sooo tiiired of thiiis looong daaay '),
      fill('gun ' + 'x '.repeat(40)),
      fill('scratch a b c my arms a b c d e until '),
      fill('lost a b c d to lost my brother to suicide '),
      fill('a'),
      fill('\n'),
    ];
    for (const input of inputs) scanCrisis(input); // let the regular expressions compile
    for (const input of inputs) {
      const started = process.hrtime.bigint();
      scanCrisis(input);
      const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
      // Measured at 1 to 4 ms. A quadratic scan of 20,000 characters takes
      // seconds, so this bound has room for a slow machine and still fails
      // on the thing it guards against.
      expect(elapsedMs, input.slice(0, 40)).toBeLessThan(200);
    }
  });
});

describe('crisisTemplate: who to call', () => {
  it('names the emergency number first for a medical emergency', () => {
    const text = crisisTemplate('Haven', ['acute-medical']);
    expect(text).toMatch(/911/);
    expect(text.indexOf('911')).toBeLessThan(text.indexOf('988'));
  });

  it('names the emergency number when someone may be in danger from another person', () => {
    expect(crisisTemplate('Haven', ['abuse'])).toMatch(/911/);
    expect(crisisTemplate('Haven', ['harm-to-others'])).toMatch(/911/);
  });

  it('has its own opening for harm to others, and still points to 988', () => {
    const text = crisisTemplate('Haven', ['harm-to-others']);
    expect(text).toMatch(/988/);
    expect(text).not.toBe(crisisTemplate('Haven', []));
    expect(text).not.toContain(String.fromCharCode(0x2014));
  });

  it('keeps the suicide script free of the emergency number', () => {
    expect(crisisTemplate('Haven', ['suicide'])).not.toMatch(/911/);
  });
});
