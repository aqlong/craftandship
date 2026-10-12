/**
 * Crisis detection: a deterministic regex scan for suicidal ideation (direct
 * and passive), plans and means, self-harm, harm to others, abuse and assault
 * disclosures, and acute medical emergencies.
 *
 * On any hit:
 * - The runtime SHORT-CIRCUITS the Claude call and returns the crisis
 *   script template.
 * - The turn is logged with rail_flags=['crisis', ...categories] for the
 *   operator to review.
 * - Memory capture is SKIPPED for that turn (the turn row itself is still
 *   stored, flagged, so the operator can review it).
 * - The runtime emits a fire-and-forget alert email to ADMIN_EMAIL so a
 *   person can follow up.
 *
 * What it is and is not
 * =====================
 * This is a list of regular expressions over normalized text. It recognizes
 * the phrasings it was written for and nothing else: it has no model of
 * meaning, it reads English only, and a disclosure worded in a way no pattern
 * anticipates passes through to the ordinary turn. The measured recall and
 * the phrasings it is known to miss are in
 * tests/unit/companion/crisis_corpus.test.ts, which scores the scan against a
 * corpus that was not derived from these patterns.
 *
 * Which way it errs
 * =================
 * A false positive costs one supportive message; a miss costs more, because a
 * missed message goes on to the rate limiters, the email gate and the cost
 * caps, any of which can answer a person in crisis with a refusal. So an
 * ambiguous phrasing ("I can't go on", "I don't want to be here anymore") is
 * flagged. Two kinds of match are deliberately left out, because the product's
 * users are healers, therapists and coaches describing their work:
 *
 *  - plain idiom ("a stroke of luck", "to die for", "cut myself some slack",
 *    "hit me up", "overdosed on chocolate", "career suicide");
 *  - a practitioner describing their practice in general terms ("I work with
 *    survivors of domestic violence", "I refer people who are suicidal to a
 *    psychiatrist", "my brother died by suicide, which is why I do grief
 *    work"). See "Context that suppresses a match" below.
 *
 * A practitioner who reports a specific person in danger now ("my client just
 * told me she is going to kill herself") does get the script. The scan cannot
 * tell who is at risk, the script costs one message, and a crisis line is
 * also where someone worried about another person can call.
 *
 * Normalization
 * =============
 * Matching runs on a normalized copy (see normalizeForCrisisScan): lower
 * case, typographic apostrophes and dashes folded to ASCII, zero-width
 * characters removed, every run of blanks collapsed to one separator (a
 * space, or a line break where the message had one; a phrase matches across
 * either), stretched letters squeezed ("diiie"), contractions and
 * "wanna/gonna" expanded ("don't" and "dont" both become "do not"), and
 * filler adverbs removed ("just", "really", "honestly", "kind of"). The
 * patterns below are therefore written in that expanded, lower-case form
 * with single spaces. The original text is what gets stored and shown.
 *
 * Context that suppresses a match
 * ===============================
 * Each category has two groups of patterns:
 *
 *  - `disclosure`: the pattern is itself a statement that someone is in
 *    danger ("kill myself", "he hits me", "she is going to kill herself").
 *  - `topical`: the pattern only names a subject ("suicidal", "self-harm",
 *    "domestic violence", "chest pain").
 *
 * A match is suppressed, within its sentence, by:
 *
 *  - a screening phrase BEFORE it ("people who", "I refer", "not for",
 *    "beyond my scope"), unless a first-person word or a clause break sits
 *    between the phrase and the match ("people who love me would be better
 *    off if I kill myself" still fires);
 *  - for topical patterns only, a screening phrase after it, or a
 *    professional-context phrase on either side ("workshop on", "trained in",
 *    "survivors of", "clients with"), under the same in-between rule;
 *  - a direct negation ("I would never", "I am not", "no thoughts of"),
 *    unless the sentence then takes it back ("... but sometimes I want to",
 *    "... until now") or hedges it ("I can not promise I will not").
 *
 * One rule works differently. Planning is often spoken of without naming
 * the plan ("I wrote my note, I just need to pick the day"). Each such cue is
 * ordinary alone, so two different cues in one message count as a suicide
 * disclosure (see PLANNING_CUES), and one cue alone does not.
 *
 * Bounded work
 * ============
 * The HTTP route runs this scan ahead of its rate limiters, so its cost is
 * bounded here and not by the caller: only the first MAX_CRISIS_SCAN_CHARS
 * characters are read, every pattern is linear in the text (no nested
 * quantifiers; gaps between words are counted), context is weighed for at
 * most MAX_BOUNDARY_CHECKS_PER_SCAN matches before the next match is simply
 * treated as a hit, and each weighing looks at most CONTEXT_WINDOW_CHARS to
 * either side of its match.
 */

import { normalizeForScan } from './scan_text';

/** Characters of a message the scan reads. The rest is ignored. */
export const MAX_CRISIS_SCAN_CHARS = 20_000;

// ---------------------------------------------------------------------------
// Normalization
// ---------------------------------------------------------------------------

// Hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar
// and the minus sign, built from code points so this source holds no literal
// dash look-alikes.
const DASH_LIKE = new RegExp(
  `[${String.fromCharCode(0x2010)}-${String.fromCharCode(0x2015)}${String.fromCharCode(0x2212)}]`,
  'g'
);

// CR, LF and the Unicode line and paragraph separators, also from code points
// (a literal separator may not appear inside a regular expression literal).
const LINE_BREAKS = new RegExp(
  `[\\r\\n${String.fromCharCode(0x2028)}${String.fromCharCode(0x2029)}]+`,
  'g'
);

/** Words whose stretched spellings ("diiie", "killll", "wannnna") are restored. */
const STRETCHABLE_WORDS = [
  'die', 'died', 'dying', 'dead', 'death', 'kill', 'killing', 'killed', 'suicide', 'suicidal',
  'myself', 'wanna', 'gonna', 'want', 'end', 'life', 'live', 'living', 'alive', 'hurt', 'hurting',
  'cut', 'cutting', 'harm', 'pills', 'overdose', 'help', 'here', 'gone', 'hate', 'breathe', 'hit',
  'hits', 'beat', 'beats', 'raped', 'scared', 'afraid', 'please', 'so', 'really', 'not', 'no',
  'never', 'me', 'off', 'all', 'too', 'tired', 'done', 'bleeding', 'stop', 'anymore', 'cant', 'dont',
];
const squeeze = (word: string): string => word.replace(/([a-z])\1+/g, '$1');
const UNSTRETCHED = new Map(STRETCHABLE_WORDS.map((word) => [squeeze(word), word]));
const HAS_STRETCH = /([a-z])\1\1/;

/** "diiie" becomes "die"; an unknown stretched word keeps two of the letter. */
function unstretchWord(word: string): string {
  if (!HAS_STRETCH.test(word)) return word;
  return UNSTRETCHED.get(squeeze(word)) ?? word.replace(/([a-z])\1{2,}/g, '$1$1');
}

/** Common ways of spelling around a filter, folded back to the plain word. */
const OBFUSCATIONS: Array<[RegExp, string]> = [
  [/\bk[*!1][l1]{2}(?=\b|ing|ed|s)/g, 'kill'],
  [/\bd[*!1]e\b/g, 'die'],
  [/\bs(?:u|\*)(?:i|1|!|\*)?[cs](?:i|1|!|\*)?d(?=e\b|es\b|al\b)/g, 'suicid'],
  [/\bsewer ?slide\b/g, 'suicide'],
];

/** Contractions, with and without the apostrophe, and informal verb forms. */
const EXPANSIONS = new Map<string, string>(Object.entries({
  "i'm": 'i am', im: 'i am', "i've": 'i have', ive: 'i have', "i'd": 'i would', "i'll": 'i will',
  "he's": 'he is', hes: 'he is', "she's": 'she is', shes: 'she is', "it's": 'it is',
  "that's": 'that is', thats: 'that is', "there's": 'there is', theres: 'there is',
  "what's": 'what is', whats: 'what is', "who's": 'who is', "here's": 'here is',
  "they're": 'they are', theyre: 'they are', "we're": 'we are', "you're": 'you are', youre: 'you are',
  "they'd": 'they would', "he'd": 'he would', "she'd": 'she would', "we'd": 'we would', "you'd": 'you would',
  "they'll": 'they will', "he'll": 'he will', "she'll": 'she will', "we'll": 'we will', "you'll": 'you will',
  "they've": 'they have', "we've": 'we have', "you've": 'you have',
  "don't": 'do not', dont: 'do not', "doesn't": 'does not', doesnt: 'does not',
  "didn't": 'did not', didnt: 'did not', "can't": 'can not', cant: 'can not', cannot: 'can not',
  "won't": 'will not', wont: 'will not', "wouldn't": 'would not', wouldnt: 'would not',
  "couldn't": 'could not', couldnt: 'could not', "shouldn't": 'should not', shouldnt: 'should not',
  "isn't": 'is not', isnt: 'is not', "aren't": 'are not', arent: 'are not',
  "wasn't": 'was not', wasnt: 'was not', "weren't": 'were not', werent: 'were not',
  "haven't": 'have not', havent: 'have not', "hasn't": 'has not', hasnt: 'has not',
  "hadn't": 'had not', hadnt: 'had not', "ain't": 'is not', aint: 'is not',
  wanna: 'want to', gonna: 'going to', gunna: 'going to', finna: 'going to', gotta: 'got to',
  tryna: 'trying to', imma: 'i am going to', "i'mma": 'i am going to', ima: 'i am going to',
  lemme: 'let me', outta: 'out of', noone: 'no one', themself: 'themselves',
}));
const WORD = /[a-z']+/g;

/** Two-word spellings joined, and digits or letters standing in for words. */
const PHRASE_FIXES: Array<[RegExp, string]> = [
  [/\bany more\b/g, 'anymore'],
  [/\b(my|him|her|your) self\b/g, '$1self'],
  [/\bthem selves\b/g, 'themselves'],
  [/\bgood[- ]bye/g, 'goodbye'],
  [/\b(want|going|need|ready|about|have|trying) 2 (?=[a-z])/g, '$1 to '],
  [/\bto b\b/g, 'to be'],
  // "ill" and "id" are words of their own, so they are read as "I will" and
  // "I would" only in front of a verb that makes them so.
  [/\bill (?=(?:hurt|kill|harm|die|do|end|never|be|take|jump|hang|shoot|cut|go|not)\b)/g, 'i will '],
  [/\bid (?=(?:rather|be|never|kill|die|love|like)\b)/g, 'i would '],
];

/**
 * Words that change how strongly something is said and not what is said.
 * Removing them lets one pattern cover "I want to die", "I just want to
 * die", "I really, honestly want to die". Negations and words that change the
 * meaning ("never", "not", "almost") are not in this list.
 */
const FILLERS =
  /\b(?:just|really|honestly|literally|actually|seriously|truly|basically|simply|totally|genuinely|definitely|kinda|sorta|kind of|sort of|fucking|fuckin|freaking|frickin|effing|bloody|damn|goddamn|even|still|sometimes|often|always|constantly|so|very|lowkey|highkey|tbh|ngl|already|finally)\b ?/g;

/**
 * The copy of `text` the patterns are matched against. Exported for tests.
 *
 * Words are separated by exactly one character: a space, or "\n" where the
 * message had a line break (any run of them, with the blanks around it). The
 * line break is kept because it does two jobs: a phrase may run across it ("I
 * want to\nkill myself" matches), and it ends a sentence for the context
 * rules, so a screening phrase on one line does not explain away a
 * disclosure on the next.
 */
export function normalizeForCrisisScan(text: string): string {
  let out = normalizeForScan(text).toLowerCase().replace(DASH_LIKE, '-');
  out = out.replace(LINE_BREAKS, '\n');
  // Trailing-off dots join the words around them: "i just... want to die".
  out = out.replace(/\.{2,}/g, ' ');
  for (const [pattern, replacement] of OBFUSCATIONS) out = out.replace(pattern, replacement);
  // Emphasis marks, quotes and brackets carry no words. Then every run of
  // blanks other than a line break becomes one space.
  out = out.replace(/[*_~"()[\]{}<>|\\/]+/g, ' ').replace(/[^\S\n]+/g, ' ');
  if (HAS_STRETCH.test(out)) out = out.replace(WORD, unstretchWord);
  // A possessive or a contraction that is not listed ("mom's", "husband's")
  // is left as written.
  out = out.replace(WORD, (word) => EXPANSIONS.get(word) ?? word);
  for (const [pattern, replacement] of PHRASE_FIXES) out = out.replace(pattern, replacement);
  out = out.replace(FILLERS, '');
  return out.replace(/ *\n[ \n]*/g, '\n').replace(/ {2,}/g, ' ').trim();
}

// ---------------------------------------------------------------------------
// Patterns (matched against normalized text: lower case, one separator
// between words, contractions expanded, fillers removed)
// ---------------------------------------------------------------------------

interface CategoryPatterns {
  topical: RegExp[];
  disclosure: RegExp[];
}

/** The one separator between two words of normalized text. */
const WORD_GAP = '[ \\n]';
const SPACE_INSIDE_CLASS = /\[[^\]]* [^\]]*\]/;

/**
 * Compiles a pattern written with plain spaces. Each space is turned into
 * WORD_GAP, so every pattern matches a phrase that runs across a line break.
 * A space inside a character class would be mangled by that, so it is refused
 * when the module loads.
 */
function rx(source: string): RegExp {
  if (SPACE_INSIDE_CLASS.test(source)) {
    throw new Error(`crisis pattern has a space inside a character class: ${source}`);
  }
  return new RegExp(source.replace(/ /g, WORD_GAP));
}

/** People whose violence toward the writer is an abuse disclosure. */
const HOME_RELATION =
  '(?:partner|husband|wife|boyfriend|girlfriend|spouse|ex|ex-husband|ex-wife|ex-boyfriend|ex-girlfriend|fiance|fiancee|dad|father|stepdad|stepfather|step-dad|step-father|mom|mum|mother|stepmom|stepmother|parent|parents|brother|sister|sibling|son|daughter|uncle|aunt|cousin|grandfather|grandpa|grandmother|grandma|roommate)';
const RELATION = `(?:${HOME_RELATION.slice(3, -1)}|boss|manager|coworker|in-law|in law|coach|teacher|neighbor|neighbour|landlord|caregiver|carer|babysitter|pastor|priest|doctor|therapist)`;
/** "my husband", "her dad", "my mom's boyfriend", and the bare noun. */
const RELATION_SUBJECT = `(?:(?:my|his|her|their) (?:own )?)?${RELATION}(?:'s (?:new )?(?:boyfriend|girlfriend|husband|wife|partner|friend|brother|dad|father))?`;
/** Words allowed between a subject and its verb. A closed list: no negations. */
const AUXILIARIES = '(?: (?:is|was|has|had|keeps|kept|would|will|used to|started|has been|had been|again|also|then|regularly|repeatedly)){0,3}';
const VIOLENCE_VERB =
  '(?:hits|hit|hitting|beats|beat|beating|hurts|hurt|hurting|raped|rapes|raping|abuses|abused|abusing|strangl\\w+|chok\\w+|punch\\w+|slap\\w+|kicks|kicked|kicking|threatens|threatened|threatening|molested|assaulted|attacked)';
/**
 * What may not follow "<verb> me": "hit me up", "beat me to it", "beat me at
 * chess", "kicked me out", "it hit me that", "hit me like a truck", and "hit
 * me with <anything that is not an object one is struck with>" ("hit me with
 * a great idea").
 */
const NOT_FIGURATIVE_ME =
  '(?! up\\b| out\\b| off\\b| to it\\b| to the\\b| that\\b| how\\b| like a\\b| at \\w| by \\w| with (?!(?:a |an |the |his |her |their |some )?(?:belt|fists?|hands?|stick|bat|bottle|chair|cord|rod|whip|hammer|knife|gun|car|shoe|pan|iron|object|something|weapon)\\b))';
/**
 * "die for you", "die of embarrassment", "die laughing": not about dying.
 * ("wanting to die for three weeks" is, so only "for <someone or something>"
 * is excluded.)
 */
const NOT_DIE_IDIOM =
  '(?! (?:for (?:you|him|her|them|this|that|my|the|a|an|love)\\b|of (?:embarrassment|laughter|shame|boredom|cuteness|happiness|curiosity|jealousy|envy|excitement|joy|mortification|cringe|old age)|laughing|happy\\b|trying\\b|on (?:this|that) hill|peacefully|well\\b|with dignity|at home\\b))';
/** "live in the city", "be here at nine", "live with my sister": about a place. */
const NOT_A_PLACE =
  '(?! (?:in|at|near|on|by|there|downtown|abroad|upstairs|alone|together|off|above|next|close|far|under|with (?:my|him|her|them|a|an|roommates|anyone|someone|people|other|others|family))\\b)';
/** "end it with a call to action": about where something ends. */
const NOT_A_PLACEMENT = '(?! (?:with|on|by|at|in|before|after|so|when|there|here)\\b)';
/** "overdosed on chocolate". */
const NOT_BENIGN_OVERDOSE =
  '(?! on (?:chocolate|sugar|candy|caffeine|coffee|carbs|cake|cookies|pizza|netflix|tv|sweets|content|cuteness|social media|instagram|tiktok|news|podcasts|zoom|meetings|screen time|sun|vitamins?|fonts|colors|glitter)\\b)';
/** Accidents and idioms after "cut myself", "burned myself", "hurt myself". */
const NOT_AN_ACCIDENT =
  '(?! (?:some slack|a break|a deal|off\\b|loose|short|shaving|cooking|chopping|slicing|dicing|lifting|moving|carrying|gardening|opening|by accident|accidentally|while|during|on (?:the|a|an|some) |on my (?!arms?\\b|legs?\\b|thighs?\\b|wrists?\\b|stomach\\b|skin\\b|body\\b)|at the gym|working out|exercising|running|skiing|hiking|climbing|training|falling|in the foot|playing|doing (?:yoga|pilates|crossfit|a |the )))';
const DRUG =
  '(?:pills|tablets|meds|medication|medications|medicine|painkillers|antidepressants|tylenol|acetaminophen|paracetamol|ibuprofen|aspirin|xanax|benzos|opioids|insulin)';
/** An amount that is not a dose: "all my", "a whole bottle of", "too many". */
const TOO_MUCH =
  '(?:all (?:of )?(?:my|the|these|those)|a (?:whole |full |entire )?(?:bottle|bunch|handful|box|pack|lot|load|ton) of|too many(?: of)?|(?:way )?too much(?: of)?|the (?:whole|entire|rest of the) (?:bottle|box|pack)(?: of)?|every (?:single )?one of|\\d{2,}|dozens of)';
/** Someone a person could hurt: "him", "someone", "my kids", "my boss". */
const PERSON_OBJECT = `(?:him|her|them|someone|somebody|people|everyone|everybody|a person|(?:my|the|that|this|his|her|their) (?:\\w+ )?(?:${RELATION.slice(3, -1)}|kids?|child|children|baby|babies|family|coworkers?|colleagues?|classmates?|students?|clients?|neighbors?|friends?))`;
/** "I am going to kill my husband for forgetting the milk, lol". */
const NOT_A_JOKE =
  '(?! (?:for (?:forgetting|losing|eating|leaving|making|telling|being|not|this|that)|lol|haha|lmao|if (?:he|she|they) (?:forgets?|loses?|eats?|leaves?|makes?|tells?|does|keeps?))\\b)';

const SUICIDE_PATTERNS: CategoryPatterns = {
  disclosure: [
    // "kill myself", "killing myself", "tried to kill myself"
    rx('\\bkill(?:ing|ed|s)? myself\\b'),
    // "end my life", "taking my own life"
    rx('\\b(?:end|ending|ended|take|taking|took|taken) my (?:own )?life\\b'),
    // "I want to die", "he wants to die", "ready to die", "deserve to die"
    rx(`\\b(?:want|wanted|wants|wanting|wish|wished|wishing|ready|need|deserve|waiting|long|longing|would like|would love|plan|planning) to die\\b${NOT_DIE_IDIOM}`),
    // "I hope I die", "wish I could die"
    rx(`\\b(?:hope|wish|pray) (?:that )?i (?:would |could |will |can |might )?die\\b${NOT_DIE_IDIOM}`),
    // "want to be dead", "wish I was dead", "wish I had never been born", "rather be dead"
    rx('\\b(?:want|wish|ready|deserve|hope|prefer) to be dead\\b'),
    rx('\\bwish(?:ed|ing)? i (?:was|were) (?:dead|never born|not (?:alive|here|born))\\b'),
    rx('\\bwish(?:ed|ing)? i had never been born\\b'),
    rx('\\b(?:better|rather) (?:off )?(?:be )?dead\\b'),
    rx('\\bunaliv(?:e|ing|ed) myself\\b'),
    // "off myself", "top myself", "do myself in", "do something to myself"
    rx('\\b(?:off|offing|offed|top|topping|topped|end|ending|delete|deleting|erase|erasing) myself\\b'),
    rx('\\bdo(?:ing)? myself in\\b|\\b(?:do|doing|did) something (?:\\w+ )?to myself\\b'),
    // "nothing to live for", "I can not do this life anymore", "I should not be alive"
    rx('\\bnothing (?:left )?to live for\\b'),
    rx('\\b(?:can not|not want to) do (?:this )?life\\b'),
    rx('\\bshould not (?:be alive|exist|have been born)\\b'),
    // "I am suicidal", "I feel suicidal", "I think I am suicidal"
    rx('\\bi (?:am|feel|felt|have been|was|get|got|am feeling|have been feeling|think i am|might be|am becoming) (?:(?!not\\b|never\\b)\\w+ )?suicidal\\b'),
    // "I do not want to live / be alive / be here / exist / wake up"
    rx(`\\b(?:do|did|does) not (?:think i )?want to (?:live|be alive|exist|be here|be around|wake up|go on living|keep living|be on this (?:earth|planet)|be in this world)\\b${NOT_A_PLACE}`),
    rx(`\\bno longer want to (?:live|be alive|exist|be here|be around)\\b${NOT_A_PLACE}`),
    rx('\\bwant to (?:not be alive|not exist|not be here|stop living|stop existing|cease to exist|not live|not wake up|never wake up|be gone forever|sleep forever|go to sleep forever)\\b'),
    // "I do not know if I want to be here anymore"
    rx('\\b(?:do not know|not sure|unsure|idk) (?:if|whether|that) i (?:want to|can|should) (?:be here|be alive|live|keep living|go on|keep going|stay alive)\\b'),
    // "I wish I would not wake up", "go to sleep and never wake up"
    rx('\\b(?:wish|hope|pray|wishing|hoping|praying) (?:that )?i (?:would not|will not|do not|did not|could not|never|would never|can not) wake up\\b'),
    rx('\\b(?:go to sleep|fall asleep|sleep|close my eyes) and (?:never|not) wake up\\b'),
    // "better off without me", "happier if I were dead", "easier if I was not here"
    rx('\\b(?:better|happier|easier|freer|safer|relieved|glad) (?:off )?(?:without me\\b(?! \\w+ing\\b)|if i (?:was|were|am) (?:not |no longer )?(?:gone|dead|here|around|alive|born|there)\\b|if i (?:died|disappeared|vanished|did not exist|never existed|was never born|were never born|had never been born|killed myself)\\b)'),
    // "I would be fine if it all ended"
    rx(`\\b(?:fine|okay|ok|alright|relieved|glad|at peace) if (?:it all|everything|my life) (?:ended|was over|were over|stopped)\\b${NOT_A_PLACEMENT}`),
    // "no reason to live", "what is the point of living", "no point in going on"
    rx(`\\b(?:no|not (?:see|seeing|find|finding) (?:a|any|the|much)|what is the|is there (?:a|any)|lost (?:the|any|my|all)) (?:reason|point|purpose) (?:to|in|of) (?:live|living|life|go on|going on|keep going|keep living|keep on living|be alive|being alive|stay alive|staying alive|carry on|carrying on|be here|being here)\\b${NOT_A_PLACE}`),
    rx('\\bnot worth living\\b'),
    rx('\\b(?:life|living) is (?:not worth it|pointless|meaningless)\\b'),
    // "I can not go on", "I do not think I can keep going" (but not "go on vacation")
    rx('\\b(?:can not|do not think i can|not sure i can|do not know if i can|do not know how (?:much longer )?i can|can no longer|not think i can) (?:go on|keep going|carry on|keep living|continue living|live like this)\\b(?=$|[.,!?;\\n]| anymore| any longer| like this(?! with)| living| with (?:life|living)| much longer)'),
    // "not sure how much longer I can keep going", "... keep doing this whole being alive thing"
    rx('\\bhow much longer i can (?:go on|keep going|carry on|hold on|keep living|stay alive|live like this)\\b'),
    rx('\\bhow much longer i can(?: \\w+){1,5} (?:being alive|living|life|alive)\\b'),
    // "tired of living", "done with life", "giving up on life"
    rx('\\b(?:tired|sick|exhausted|weary) of (?:living|being alive|life|existing|this life|my life|being here)\\b(?! (?:in|at|on|with)\\b)'),
    rx('\\b(?:done|finished|through) with (?:life|living|this life|this world|being alive)\\b'),
    rx('\\bgiv(?:e|ing|en) up on (?:life|living)\\b'),
    // "I do not see a way out", "the only way out" (but not "a way out of this lease")
    rx('\\b(?:no|not see (?:a|any|another)|can not see (?:a|any|another)|not seeing (?:a|any)|only|no other) way out\\b(?! of\\b)'),
    // "I want it all to end", "I want the pain to stop"
    rx(`\\bi want (?:it all|all of it|all of this|everything|the pain|the hurt|the suffering|my pain|my suffering|my life|this pain) to (?:end|stop|be over|go away)\\b${NOT_A_PLACEMENT}`),
    // "I wish I could disappear", "want to disappear forever"
    rx('\\bwish(?:ed|ing)? i could (?:disappear|vanish|stop existing|not exist|cease to exist|fade away)\\b(?! (?:to|for|into|on|from (?:social|instagram|the internet|facebook))\\b)'),
    // "I do not care if I never wake up", "I do not care if I live or die"
    rx('\\b(?:not care|not mind) (?:if|whether) i (?:die|died|live|lived|live or die|never wake up|do not wake up|did not wake up|wake up)\\b'),
    // "I am not coming back from this one" (but not "coming back to the studio")
    rx('\\bi (?:am|will) not (?:be )?coming back\\b(?! (?:to|until|till|before|for|next|on|in|after|from (?:vacation|holiday|lunch|the|my|maternity))\\b)'),
    rx('\\b(?:want|wish|going) to (?:disappear|vanish) (?:forever|for good|completely|from (?:this|the) (?:world|earth|planet))\\b'),
    rx('\\bi want to disappear\\b(?! (?:to|for|into|on|in|behind|from (?:social|instagram|the internet|facebook))\\b)'),
    // "nobody would notice if I was gone", "no one would miss me if I died"
    rx('\\b(?:notice|care|miss me|mind|be sad|cry|know) if i (?:was gone|were gone|died|disappeared|vanished|was dead|were dead|killed myself|was not here|were not here|was not around|were not around|did not exist|never came back|did not wake up)\\b'),
    rx('\\bi am (?:a|such a|nothing but a|only a) burden\\b'),
    // "I will not be here much longer", "you will not have to worry about me"
    rx('\\bi will not be (?:here|around) (?:tomorrow|much longer|for long|anymore|for much longer)\\b'),
    rx('\\bwill not have to (?:worry about|deal with|put up with) me\\b'),
    // "end it all", "ending things", "I want to end it", "tonight is the night I end it"
    rx(`\\b(?:want to|going to|will|ready to|about to|decided to|need to|have to|could|should|might|might as well|thinking (?:about|of)|think about|thought about|thoughts of|time to|plan to|planning to|tempted to|trying to) end(?:ing)? (?:it all|things|everything)\\b${NOT_A_PLACEMENT}`),
    rx(`\\bending it all\\b${NOT_A_PLACEMENT}`),
    rx(`\\bi (?:want to|am going to|will|am ready to|am about to|have decided to|need to|have to|might as well|could) end it\\b${NOT_A_PLACEMENT}`),
    rx('\\bthe (?:night|day|time) i end it\\b'),
    rx(`\\b(?:thinking|thought|think|thoughts) (?:about|of) ending (?:it|things|everything|my life|it all)\\b${NOT_A_PLACEMENT}`),
    // Plans and means.
    rx('\\b(?:hang|hanging|hanged|hung|shoot|shooting|shot|drown|drowning|drowned|stab|stabbing|stabbed|poison|poisoning|poisoned|suffocate|suffocating|suffocated|electrocute|gas|gassing) myself\\b(?! in the foot)'),
    rx('\\b(?:jump|jumping|leap|leaping|throw myself|throwing myself) (?:off|from|out of|in front of) (?:a|an|the|my) (?:bridge|building|roof|rooftop|balcony|overpass|cliff|ledge|window|train|bus|truck|parking garage)\\b'),
    rx('\\b(?:step|stepping|walk|walking|jump|jumping|run|running|drive|driving) (?:out )?in(?:to| front of) (?:oncoming traffic|traffic|a train|the train|a bus|a truck|the tracks)\\b'),
    rx('\\b(?:drive|driving|crash|crashing|steer|steering|swerve|swerving) (?:my (?:car|truck) )?(?:off (?:the|a) (?:road|cliff|bridge|overpass)|into (?:a|the|oncoming) (?:tree|wall|traffic|lake|river|pole|truck|barrier))\\b'),
    rx('\\b(?:ways?|how) to (?:die|kill myself|end my life|end it all|commit suicide|overdose|hang myself|tie a noose)\\b(?! (?:for|of|laughing|happy|well)\\b)'),
    rx('\\b(?:researching|researched|googling|googled|looking up|looked up|searching for|searched for|reading about|read about|looking into) (?:\\w+ ){0,2}(?:suicide|lethal doses?|painless ways)\\b'),
    rx('\\b(?:wrote|written|writing|write|left|leaving|drafted|drafting) (?:\\w+ ){0,2}(?:goodbye|farewell) (?:letters?|notes?|messages?|texts?|emails?)\\b'),
    rx(`\\b(?:take|taking|swallow|swallowing|down|downing) ${TOO_MUCH}(?: \\w+){0,3} ${DRUG}\\b`),
    rx(`\\b(?:saving up|saved up|stockpiling|stockpiled|hoarding|hoarded|been saving|collecting|collected) (?:up )?(?:my |the |enough |all my )?(?:sleeping )?${DRUG}\\b`),
    rx('\\b(?:have|got) (?:the|my|enough) (?:pills|gun|rope|razor|blades?|noose|knife)(?: \\w+){0,2} ready\\b'),
    // Giving belongings away is a named warning sign ("I gave away my things")
    rx('\\b(?:gave|giving|given) away (?:all (?:of )?)?my (?:things|belongings|stuff|possessions|(?:favorite|favourite|prized|most \\w+) \\w+)\\b'),
    // "I have the pills saved up"
    rx(`\\b(?:have|got) (?:the|my|enough|all the|all my) ${DRUG} (?:saved up|saved|stockpiled|hoarded|stashed|set aside|counted out|lined up)\\b`),
    // "I have a gun in the house and I have been thinking about using it on
    // myself" (a weapon, then "use it on myself", in one sentence)
    rx('(?<!massage |glue |heat |nail |staple |spray |tattoo |caulk )\\b(?:gun|pistol|rifle|shotgun|firearm|knife|razor|blade|rope|noose)\\b[^.!?]{0,80}\\bus(?:e|ing) it on myself\\b'),
    rx(`\\b(?:going to|want to|about to|will|plan to|planning to|trying to|tried to|ready to|thinking (?:about|of)) overdos(?:e|ing)\\b${NOT_BENIGN_OVERDOSE}`),
    // "kms" as shorthand, but not a distance ("30 kms")
    rx('(?:^|\\n|[.!?,;] |\\b(?:i|to|will|might|should|or|and|then|could|can) )kms(?=$|[.!?,;\\n]| )'),
    // Someone else in danger now: "she is going to kill herself", "he wants to end his life"
    rx('\\b(?:going to|wants to|want to|threatening to|threatened to|trying to|tried to|plans to|plan to|planning to|might|may|will|ready to|about to|talking about|talks about|thinking about|thinks about|thinking of|thoughts of|attempted to|intends to) (?:kill|killing|hurt|hurting|harm|harming|hang|shoot) (?:himself|herself|themselves)\\b'),
    rx('\\b(?:going to|wants to|want to|threatening to|threatened to|trying to|tried to|plans to|plan to|planning to|might|will|ready to|about to|talking about|thinking about|thinking of) (?:end|ending|take|taking) (?:his|her|their) (?:own )?li(?:fe|ves)\\b'),
    rx(`\\b(?:he|she|they) (?:is|are) going to end it\\b${NOT_A_PLACEMENT}`),
  ],
  topical: [
    // The bare words. Not: "died by suicide", "lost my brother to suicide"
    // (a past loss), "career suicide" (idiom), "suicide prevention" and the
    // other names of a field of work.
    rx('(?<!\\b(?:died|die|dying|death|deaths|dies|loss|losses|lost|bereaved|bereavement) by |\\b(?:lost|losing|lose) (?:\\w+ ){1,4}to |\\b(?:career|social|political|commercial|financial|professional|brand) )\\bsuicid(?:e|al)\\b(?! (?:prevention|awareness|rates?|statistics|stats|loss|survivors?|hotlines?|helplines?|lifeline|risk assessment|research|training|intervention|bereavement|postvention|screening|education|first aid|safety planning)\\b)'),
  ],
};

const SELF_HARM_PATTERNS: CategoryPatterns = {
  disclosure: [
    // "cutting myself", "hurt myself", "burn myself" (but not "cut myself shaving")
    rx(`(?<!accidentally )\\b(?:cut|cutting|cuts|burn|burning|burned|burnt|burns|hurt|hurting|hurts|harm|harming|harmed|harms|injure|injuring|injured|punish|punishing|punished|starve|starving|starved|hit|hitting|hits|bite|biting|scratch|scratching|scratched|punch|punching|punched|stab|stabbing|bruise|bruising|mutilate|mutilating) myself\\b${NOT_AN_ACCIDENT}`),
    // "I have been cutting again", "I can not stop cutting"
    rx('\\b(?:can not stop|been|started|start|stop|stopped|quit|back to|relapsed (?:on|with|into)(?: the| my)?|keep|kept) cutting\\b(?=$|[.,!?;\\n]| again| lately| every| when| since| to (?:cope|feel)| on my (?:arms?|legs?|thighs?|wrists?|stomach|skin))'),
    // "the urge to cut", "I want to cut so bad" (but not "to cut prices")
    rx('\\b(?:urge|urges|impulse|impulses|temptation|craving|compulsion) to (?:cut|burn|hurt|harm|hit|scratch|injure)\\b(?! (?:prices|costs|back|down|out|corners|ties|carbs|sugar|my (?:prices|rates|hours|hair)|the (?:price|cord))\\b)'),
    rx('\\b(?:want|wanting|need|needing|tempted|itching|craving) to (?:cut|burn)\\b(?=$|[.,!?;\\n]| bad| badly| again| right now| rn\\b| tonight)'),
    // "slit my wrists", "cutting my arms"
    rx('\\b(?:cut|cutting|burn|burning|burned|carve|carving|carved|slice|slicing|sliced|slash|slashing|slashed|slit|slitting) (?:open )?my (?:own )?(?:arms?|legs?|thighs?|wrists?|skin|stomach|forearms?|veins|throat)\\b'),
    // "scratch my arms until they bleed"
    rx('\\b(?:scratch|scratching|scratched|pick|picking|picked|bite|biting|bit|hit|hitting|punch|punching|punched|bang|banging|banged|dig|digging|dug|claw|clawing|clawed|pinch|pinching|pinched)(?: \\w+){0,3} my(?:self)?(?: \\w+){0,5} (?:until|till|til) (?:they|it|i) (?:bleed|bleeds|bled|bruise|bruises|leaves? (?:a )?(?:bruise|mark)s?|pass out|passed out)\\b'),
    // "I wanted to feel pain", "I deserve to be hurt"
    rx('\\bi (?:want|wanted|need|needed|deserve|deserved) to (?:feel (?:physical )?pain|be hurt|be punished|bleed)\\b'),
    // "I self harm", "I have been self-harming"
    rx('\\bi (?:have been |am |was |started |keep |used to )?self(?:-| )?harm(?:ed|ing)?\\b'),
    // Someone else: "my daughter is cutting herself", "she has been cutting again"
    rx('\\b(?:is|was|has been|keeps|started|been|are) (?:cutting|hurting|harming|burning|starving) (?:himself|herself|themselves)\\b'),
    rx('\\b(?:he|she|they|my \\w+) (?:is|has been|keeps|started) (?:cutting|self-harming|self harming)\\b(?=$|[.,!?;\\n]| again)'),
  ],
  topical: [rx('\\bself(?:-| )?(?:harm|injur|mutilat)\\w*')],
};

const ABUSE_PATTERNS: CategoryPatterns = {
  disclosure: [
    // "my husband hits me", "my dad used to beat me", "my mom's boyfriend hurts me"
    rx(`\\b${RELATION_SUBJECT}${AUXILIARIES} ${VIOLENCE_VERB} me\\b${NOT_FIGURATIVE_ME}`),
    // "he hits me", "she slapped me". A pronoun subject is a person, so
    // "hurts" here is an abuse signal, not body pain ("my back hurts").
    rx(`\\b(?:he|she|they|someone|somebody)${AUXILIARIES} ${VIOLENCE_VERB} me\\b${NOT_FIGURATIVE_ME}`),
    // "... controls my money and hits me if I ask"
    rx(`\\b(?:and|then|or|who|also) (?:hits|hitting|hit|beats|beating|beat|chokes|choking|choked|slaps|slapping|slapped|punches|punching|punched|kicks|kicking|kicked|strangles|strangling|strangled|rapes|raping|raped|abuses|abusing|abused|threatens|threatening|threatened) me\\b${NOT_FIGURATIVE_ME}`),
    // "keeps threatening me"
    rx(`\\b(?:keeps|kept|is|was|started|been) threatening me\\b${NOT_FIGURATIVE_ME}`),
    // Verbs that need no named subject: "someone assaulted me", "my uncle abused me"
    rx('\\b(?:raped|rapes|raping|molested|molesting|molests|sexually (?:assaulted|abused|harassed)|assaulted|abused|abuses|abusing|strangled|strangles|strangling|stalked|stalks|stalking|groped|groping|trafficked|drugged) me\\b'),
    // "I was raped", "I am being abused", "I was attacked and raped"
    rx('\\bi (?:was|got|have been|had been|am being|was being|am getting|keep getting|was getting) (?:(?:repeatedly|brutally|violently|physically|sexually|emotionally|verbally|badly|date|gang|once|twice|also|attacked and|beaten and|drugged and|assaulted and|kidnapped and|robbed and) ){0,2}(?:raped|molested|assaulted|abused|beaten|battered|trafficked|groomed|stalked|strangled|choked|drugged|roofied)\\b'),
    rx('\\bi (?:am|was|have been|had been|keep|kept) (?:being|getting) (?:abused|hit|hurt|stalked|beaten|threatened|harassed|followed|controlled)\\b'),
    // Threats and fear: "he threatened to kill me", "I am scared he is going to kill me"
    rx('\\bthreaten(?:s|ed|ing)? to (?:kill|hurt|murder|shoot|stab|beat|rape|harm|find|destroy|ruin) (?:me|her|him|them|my (?:kids|children|family|pets?|dog|cat|baby|mom|dad|parents|son|daughter))\\b'),
    rx('\\b(?:afraid|scared|terrified|frightened|worried|fear) (?:that )?(?:he|she|they|my \\w+) (?:is|are|will|would|might|may|could) (?:going to )?(?:kill|hurt|murder|shoot|stab|beat|rape|harm|find|hit) me\\b'),
    rx('\\b(?:said|says|told me|tells me|swore|texted|wrote)(?: that)? (?:he|she|they) (?:would|will|is going to|was going to|were going to|are going to|wants to|wanted to|could) (?:kill|hurt|murder|shoot|stab|harm|beat) me\\b'),
    rx(`\\b(?:afraid|scared|terrified|frightened|fearful) (?:of|for) (?:my (?:life|safety|own safety)|(?:my|his|her) (?:own )?${HOME_RELATION})\\b`),
    // "I am not safe at home", "I do not feel safe with my partner"
    rx(`\\b(?:not safe|not feel safe|not feeling safe|never feel safe|unsafe|never safe|no longer safe|no longer feel safe) (?:at home|in my (?:own )?(?:home|house|relationship|marriage|apartment)|(?:with|around) (?:him|her|them|my (?:own )?${HOME_RELATION}))\\b`),
    // "I am in an abusive relationship", "my husband is abusive"
    rx('\\b(?:in|stuck in|trapped in|leaving|escaping|fleeing) (?:an|my|this) (?:\\w+ )?abusive (?:relationship|marriage|home|household|situation)\\b'),
    rx(`\\b(?:my|his|her) (?:own )?${HOME_RELATION} is (?:(?:very|physically|emotionally|verbally) )?abusive\\b`),
    // Coercion: "forces me to have sex", "forced himself on me", "will not let me leave"
    rx('\\bforc(?:es|ed|ing) me to (?:have sex|sleep with|do (?:things|it|sexual)|touch|perform|watch|strip)\\b'),
    rx('\\bforc(?:es|ed|ing) (?:himself|herself|themselves) on me\\b'),
    rx('\\b(?:touches|touching|touched) me (?:when i say no|without (?:my )?(?:consent|permission)|after i (?:say|said) no|while i (?:sleep|am asleep|was asleep))\\b'),
    // Physical control: "he grabbed me by the throat", "locked me in", "puts his hands on me"
    rx(`\\b(?:he|she|they|${RELATION_SUBJECT}) (?:grabbed|grabs|dragged|drags|threw|throws|pushed|pushes) me (?:by|against|down|to the (?:ground|floor)|into (?:a|the) (?:wall|door|doorframe|table|counter|cabinet|car|corner|stairs|floor|ground)|across)\\b`),
    rx(`\\b(?:he|she|they|${RELATION_SUBJECT}) (?:shoved|shoves|slammed|slams|pinned|pins) me\\b`),
    rx(`\\b(?:he|she|they|${RELATION_SUBJECT}) (?:grabbed|grabs|squeezed|squeezes) my (?:throat|neck|hair|arm|wrist|face)\\b`),
    // "he forced me last night" (but not "forced me to rebrand")
    rx(`\\b(?:he|she|they|${RELATION_SUBJECT}) forced me\\b(?! to (?!have sex|sleep|do|touch|perform|watch|strip))`),
    // "keeps cornering me and touching me", "someone keeps following me home"
    rx('\\b(?:keeps|kept|will not stop|would not stop|started|been) (?:\\w+ ){0,7}(?:touching|groping|fondling|kissing) me\\b'),
    rx('\\bfollowing me (?:home|around|everywhere|to (?:my|the) (?:car|house|work|studio|office))\\b'),
    // Control: "he checks my phone", "tracks where I go"
    rx(`\\b(?:he|she|${RELATION_SUBJECT}) (?:checks|goes through|monitors|tracks|controls) (?:my (?:phone|messages|texts|location|every move|money|finances|bank account)|where i go|who i (?:talk to|see)|everything i do|all my money)\\b`),
    rx('\\b(?:locks|locked|locking) me (?:in|inside|up)\\b'),
    rx('\\b(?:puts|put|putting|laid|lays|laying) (?:his|her|their) hands on me\\b'),
    // "he gets violent", "he will kill me if I leave", "I am afraid to go home"
    rx(`\\b(?:he|she|${RELATION_SUBJECT}) (?:is|gets|got|was|becomes|can be|has been|turned|turns) (?:\\w+ )?violent\\b`),
    rx('\\b(?:he|she|they) will (?:kill|hurt|find|destroy) me if i (?:leave|go|tell|call|say)\\b'),
    rx('\\b(?:afraid|scared|terrified) to go home\\b'),
    rx(`\\b(?:he|she|they|${RELATION}) (?:will not|would not|does not|did not|never|refuses to) let me (?:leave|go out|out of|see my|have (?:my own |any )?money|work|talk to|use (?:my|the) phone|go anywhere|see (?:anyone|a doctor))\\b`),
    rx('\\btouched me(?= (?:when i was|inappropriately|sexually|down there|where|without|as a (?:child|kid)|at night))'),
    // Someone else: "her husband beats her", "my sister is being abused"
    rx(`\\b(?:her|his|their) (?:own )?${RELATION}${AUXILIARIES} (?:beats|hits|abuses|rapes|chokes|strangles|is beating|is hitting|is abusing|threatens|is threatening|hurts|is hurting|molested|raped|is molesting) (?:her|him|them)\\b`),
    rx('\\b(?:is|are) being (?:abused|beaten|hit|hurt|raped|molested|stalked|trafficked|assaulted|threatened)\\b'),
  ],
  topical: [
    // Not the names of a field of work: "domestic violence advocate".
    rx('\\bdomestic (?:violence|abuse)\\b(?! (?:advocate|advocacy|counselor|counsellor|shelter|survivors?|awareness|prevention|hotline|training|program|services|sector|field|work|organization|charity|nonprofit)\\b)'),
  ],
};

const ACUTE_MEDICAL_PATTERNS: CategoryPatterns = {
  disclosure: [
    rx('\\bi (?:think i am|might be|may be|am|think i might be) having an? (?:heart attack|stroke|seizure|allergic reaction|asthma attack)\\b'),
    // "I overdosed", "I took too many pills", "I swallowed a bunch of pills"
    rx(`\\bi (?:have |think i |may have |might have )?overdosed\\b${NOT_BENIGN_OVERDOSE}`),
    rx('\\b(?:took|taken|taking) an overdose\\b|\\bi am overdosing\\b'),
    rx(`\\b(?:took|taken|swallowed|downed) ${TOO_MUCH}(?: \\w+){0,3} ${DRUG}\\b`),
    // Signs of a stroke
    rx('\\b(?:face|mouth|smile) is (?:drooping|droopy|numb|sagging)\\b|\\b(?:left|right) (?:arm|side|leg) is (?:numb|weak|tingling|going numb)\\b|\\bslurr(?:ed|ing) (?:my )?(?:speech|words)\\b|\\bspeech is slurr\\w+'),
    rx('\\bmy chest (?:hurts|is hurting|aches|is tight|feels tight|feels heavy|feels crushed|is pounding)\\b'),
    rx('\\bthroat is (?:closing|swelling|tightening)\\b|\\b(?:tongue|lips|face) (?:is|are) swelling\\b'),
    rx('\\bi (?:can barely|can hardly|am struggling to|am unable to|am not able to) breathe\\b|\\bi am having (?:trouble|difficulty|a hard time) breathing\\b'),
    rx('\\bi think i am dying\\b|\\b(?:coughing|throwing|vomiting|spitting) up blood\\b|\\bvomiting blood\\b|\\bworst headache of my life\\b'),
    rx('\\bnumb(?:ness)? (?:on|down) (?:my|one|the) (?:left |right |whole |entire )?(?:side|arm|face)\\b'),
  ],
  topical: [
    // "can't breathe" (but not "can't breathe in these jeans")
    rx('\\bcan not breathe\\b(?! in (?:these|this|my|those|that|here)\\b)'),
    rx('\\bchest pains?\\b'),
    // A bare "stroke" or "overdose" is not here: both fired on idiom ("a
    // stroke of luck", "sugar overdose") and on history ("had a stroke in
    // 2015"). The emergency is "having" one.
    rx('\\bhaving an? (?:heart attack|stroke|seizure)\\b'),
    rx('\\bbleeding (?:heavily|out|badly|a lot|profusely|everywhere|and it will not stop)\\b(?! (?:money|cash)\\b)|\\b(?:will not|can not|would not) stop (?:the )?bleeding\\b'),
  ],
};

/**
 * Planning cues. Each is too ordinary to flag alone ("I picked a date", "I
 * wrote a note", "I gave away my old table"), but people who are preparing to
 * end their life often say two of them together ("I wrote my note last
 * night, I just need to pick the day") without ever naming what the plan is.
 * Two DIFFERENT cues in one message count as a suicide disclosure. They are
 * not weighed against context: the pairing is the signal.
 */
const PLANNING_CUES: RegExp[] = [
  // the note
  rx('\\b(?:wrote|written|writing|finished|left|leaving) (?:my|the|a) note\\b|\\bnote (?:is )?written\\b'),
  // the day
  rx('\\b(?:pick|picked|picking|chose|chosen|choose|decided on|set|settled on) (?:a|the|my) (?:day|date)\\b'),
  // goodbyes
  rx('\\b(?:said|saying|say) (?:my )?goodbyes?\\b|\\btold (?:\\w+ ){1,3}goodbye\\b'),
  // "it", unnamed
  rx('\\b(?:how|where|when) i am going to do it\\b|\\bdecided to do it\\b'),
  // the means, at hand
  rx('(?<!massage )\\b(?:gun|pistol|rifle|shotgun|firearm|pills|rope|razors?|blades?|noose)\\b(?: (?:is|are|out|sitting|right|here|there)){0,3} (?:on|in) (?:the|my) (?:table|nightstand|night stand|counter|bed|desk|lap|hands?|drawer|bedside|dresser)\\b|\\bhave (?:a|the|my) (?:rope|noose)\\b'),
  rx('\\bkeep (?:looking|staring) at (?:it|them)\\b'),
  // belongings
  rx('\\b(?:gave|giving|given) away my\\b'),
  rx('\\bwill not need (?:them|it|any of it|this|these)\\b'),
  // waiting to be alone
  rx('\\bwaiting for (?:the house to be empty|everyone to (?:leave|be asleep|go to sleep|be gone)|them to (?:leave|be asleep|go to sleep))\\b'),
];

function hasTwoPlanningCues(text: string): boolean {
  let seen = 0;
  for (const cue of PLANNING_CUES) {
    if (cue.test(text)) {
      seen += 1;
      if (seen === 2) return true;
    }
  }
  return false;
}

const HARM_TO_OTHERS_PATTERNS: CategoryPatterns = {
  disclosure: [
    // "thoughts of hurting my children", "thinking about killing my wife"
    rx(`\\b(?:thoughts?|urges?|fantasies|fantasy|images|visions) (?:of|about) (?:hurting|harming|killing|murdering|shooting|stabbing|strangling|smothering|drowning|shaking) ${PERSON_OBJECT}\\b`),
    rx(`\\b(?:thinking|think|thought|fantasizing|fantasize|dreaming|daydreaming) (?:about|of) (?:hurting|harming|killing|murdering|shooting|stabbing|strangling) ${PERSON_OBJECT}\\b`),
    // "I keep picturing hurting my husband"
    rx(`\\b(?:picturing|picture|imagining|imagine|envisioning|visualizing) (?:myself )?(?:hurting|harming|killing|murdering|shooting|stabbing|strangling) ${PERSON_OBJECT}\\b`),
    // "I am afraid I am going to hurt my baby"
    rx(`\\b(?:afraid|scared|worried|terrified|frightened|fear) (?:that )?i (?:am going to|will|might|may|could|would|am about to) (?:hurt|harm|kill|shake|hit|drown|smother|stab|strangle) ${PERSON_OBJECT}\\b`),
    // "I want to kill him", "I am going to hurt someone" (but not the
    // exasperated "I could kill my web guy")
    rx(`\\bi (?:want to|am going to|will|might|may|need to|have a plan to|plan to|am planning to|swear i will|intend to|am about to) (?:kill|murder|shoot|stab|strangle|hurt|harm) ${PERSON_OBJECT}\\b${NOT_A_JOKE}`),
    // "I do not trust myself around my baby"
    rx('\\bdo not trust myself (?:around|with|alone with) (?:my|the|him|her|them)\\b'),
    rx('\\bcould (?:kill|hurt|murder) (?:someone|somebody|a person)\\b'),
    // "I want my landlord dead"
    rx(`\\bwant(?:ed|s)? ${PERSON_OBJECT} dead\\b`),
    rx('\\bshoot(?:ing)? up (?:my|the|a|that|this) (?:office|school|work|workplace|church|place|store|mall|building|party|class|classroom|job)\\b'),
    rx('\\ba (?:gun|pistol|rifle|shotgun|firearm|knife) and i (?:am going to|will|want to|plan to|intend to) use it\\b'),
  ],
  topical: [rx('\\bhomicidal\\b')],
};

/**
 * Screening phrases. In the same sentence and BEFORE a match, they mean the
 * writer is describing who they do not take or whom they refer out, not their
 * own state. After a match they apply to topical patterns only.
 */
const POPULATION =
  '(?:people|person|persons|someone|anyone|somebody|anybody|everyone|folks|those|clients?|patients?|students?|women|men|teens|teenagers|adolescents|kids|children|adults|couples|families|veterans|survivors|individuals|caregivers|parents|members|participants)';
const SCREENING_PATTERNS: RegExp[] = [
  rx(`\\b${POPULATION}(?: \\w+){0,2} who(?:se)?\\b`),
  // What a group is reported to say in general: "clients tell me they ..."
  rx('\\b(?:clients|patients|people|students|women|men|teens|parents|couples|families|members|participants) (?:tell|say|report|describe|share)(?: (?:me|us))?(?: that)?\\b'),
  rx('\\bnot for\\b'),
  rx('\\bnot the right (?:fit|practitioner|person|place)\\b'),
  rx('\\bi (?:would |will |usually |typically |generally )?refer\\b'),
  rx('\\brefer (?:out|them|someone|people|clients?|folks|those|anyone|him|her)\\b'),
  rx('\\b(?:beyond|outside) my scope\\b'),
  rx('\\bnot (?:in |within )?my (?:scope|specialty|focus|expertise|wheelhouse|lane|training)\\b'),
  rx('\\bnot equipped\\b'),
  rx('\\b(?:will not|would not|do not|does not|can not) (?:take|work with|see|treat|accept)\\b'),
  rx('\\bwho needs?\\b'),
  rx('\\bif (?:you|someone|somebody|they|a client|a student|a patient|anyone) (?:are|is|need|needs|discloses|mentions|says|tells|shares|reports|expresses|has|have|shows|presents)\\b'),
];

/**
 * Professional context: the writer is talking about a field of work, a
 * population, or something they made or read. Weighed for topical patterns
 * only, on either side of the match.
 */
const PROFESSIONAL_PATTERNS: RegExp[] = [
  rx(`\\b${POPULATION} (?:with|in|experiencing|struggling with|dealing with|living with|recovering from|affected by|facing|at risk of|having|going through|where)\\b`),
  rx('\\b(?:workshops?|class(?:es)?|courses?|trainings?|talks?|lectures?|webinars?|retreats?|books?|articles?|chapters?|papers?|posts?|podcasts?|episodes?|films?|movies?|shows?|plays?|stories|story|pages?|sections?|groups?|programs?|circles?|services?|sessions?|resources?|hotlines?|helplines?) (?:on|about|for|covering|addressing|listing|around)\\b'),
  rx('\\b(?:blog posts?|dissertation|thesis|documentary|novel|the news|statistics|intake forms?|screening|screens? for|risk assessment|referral list|caseload|specialt(?:y|ies)|speciali[sz](?:e|es|ed|ing) in|trained in|training in|certified in|certification in|experience (?:with|in)|survivors? of|victims? of|counsel(?:ing|ling)? (?:for|couples|families)|therapy for|treatment for|healing from|recovery from|recovering from)\\b'),
  rx('\\bi (?:work with|counsel|treat|teach|volunteer|facilitate|support|help)\\b'),
  rx('\\bmy (?:practice|work|caseload|clients|specialty|focus|training|background|research|writing|program|group|workshop|class|book|blog|podcast) (?:focuses|centers|centres|is focused|specializes|includes|covers|involves|addresses|deals|is (?:mostly|mainly|largely|all|about|on|in|with|for)|are (?:mostly|mainly|largely|survivors|people|women|men|teens)|was (?:on|about|in))\\b'),
];

/**
 * First-person words. If one sits between a context phrase and the match,
 * the sentence is the writer talking about themselves ("people who love me
 * would be better off if I kill myself") and the phrase does not apply.
 */
const FIRST_PERSON_MARKER = /\b(?:i|me|my|mine|myself)\b/;
/** A new clause between a context phrase and the match also ends its reach. */
const CLAUSE_BREAK =
  /[;:]| but | however | though | although | yet |, (?:and |so |because |or )?(?:he|she|they|we|it|life|living|everything|nothing|there|that|this|nobody|no one|everyone)\b/;

/**
 * A negation that governs the match directly: "would never [hurt myself]",
 * "am not [suicidal]", "no thoughts of [harming myself]", "not going to
 * [kill myself]". Anchored at the end of the text before the match; "why not"
 * is not a negation.
 */
const NEGATED_PREFIX =
  /(?:^|[ ,])(?<!why )(?:not|never|no)(?: (?:ever|going to|want to|wanted to|planning to|plan to|trying to|thinking about|thinking of|thought about|thought of|thoughts of|thoughts about|plans to|intention of|intent to|been|being|having|have|had|felt|feeling|feel|a|an|any|is|was|like i|said i|said that i|thought i|at risk of|in danger of|currently|actively)){0,3} $/;
/** After a negated match, words that take the negation back. */
const NEGATION_REVERSED =
  /\b(?:until|unless|yet|so far|this time|today|tonight|right now|before (?:this|now|today|tonight)|but (?:i (?:think|thought|want|wanted|might|may|could|have|am|was|would|will|keep|did)|it (?:is|has|crossed)|the thought|sometimes|lately|now|honestly|maybe|part of me))\b/;
/** Before a negated match, words that make the negation a hedge. */
const NEGATION_HEDGED = /\b(?:can not promise|not promise|not sure|do not know (?:if|whether)|no guarantee|what if i)\b/;
const NEGATION_WINDOW_CHARS = 120;

export type CrisisCategory =
  | 'suicide'
  | 'self-harm'
  | 'abuse'
  | 'acute-medical'
  | 'harm-to-others';

export interface CrisisScanResult {
  flagged: boolean;
  categories: CrisisCategory[];
}

const CATEGORIES: Array<[CrisisCategory, CategoryPatterns]> = [
  ['suicide', SUICIDE_PATTERNS],
  ['self-harm', SELF_HARM_PATTERNS],
  ['abuse', ABUSE_PATTERNS],
  ['acute-medical', ACUTE_MEDICAL_PATTERNS],
  ['harm-to-others', HARM_TO_OTHERS_PATTERNS],
];

/**
 * Returns flagged=true if any pattern matches AND the match is not explained
 * away by its context. Several categories may fire at once (for example
 * suicide and self-harm).
 *
 * Matching is regex-only and does not depend on any network call, so it runs
 * the same way when the model or the database is down. Only the first
 * MAX_CRISIS_SCAN_CHARS characters are read.
 */
export function scanCrisis(text: string): CrisisScanResult {
  const bounded = text.length > MAX_CRISIS_SCAN_CHARS ? text.slice(0, MAX_CRISIS_SCAN_CHARS) : text;
  const normalized = normalizeForCrisisScan(bounded);
  const budget: BoundaryCheckBudget = { remaining: MAX_BOUNDARY_CHECKS_PER_SCAN };
  const categories: CrisisCategory[] = [];
  for (const [category, patterns] of CATEGORIES) {
    if (categoryFires(normalized, patterns, budget)) categories.push(category);
    else if (category === 'suicide' && hasTwoPlanningCues(normalized)) categories.push(category);
  }
  return { flagged: categories.length > 0, categories };
}

/**
 * How many pattern matches one scan weighs against their context before it
 * stops weighing and treats the next match as a hit.
 *
 * Each weighing runs every context pattern over the match's sentence, so a
 * text built from hundreds of suppressed triggers ("people who ... suicide
 * suicide suicide ...") costs time quadratic in its length. The HTTP route
 * runs this scan ahead of its rate limiters, so that cost must be bounded
 * here. Real messages mention a trigger a handful of times; past this many
 * suppressed mentions the text gets the crisis script, which is the direction
 * this module already errs in.
 */
export const MAX_BOUNDARY_CHECKS_PER_SCAN = 24;

interface BoundaryCheckBudget {
  remaining: number;
}

interface PatternMatch {
  start: number;
  end: number;
}

/**
 * A category fires only if at least one pattern match is NOT suppressed by
 * its context. If every match in the text is suppressed, the category is
 * suppressed, as long as the scan's budget of context checks lasts (see
 * MAX_BOUNDARY_CHECKS_PER_SCAN); after that the next match fires.
 */
function categoryFires(
  text: string,
  group: CategoryPatterns,
  budget: BoundaryCheckBudget
): boolean {
  for (const [patterns, isDisclosure] of [
    [group.disclosure, true],
    [group.topical, false],
  ] as Array<[RegExp[], boolean]>) {
    for (const p of patterns) {
      // One more than the budget is enough: the match after the last one
      // weighed fires without being weighed.
      for (const m of findMatches(text, p, budget.remaining + 1)) {
        if (budget.remaining <= 0) return true;
        budget.remaining -= 1;
        if (!isSuppressedByContext(text, m, isDisclosure)) return true;
      }
    }
  }
  return false;
}

/** Compiled once per pattern: the global form that exec() walks a text with. */
const GLOBAL_FORM = new WeakMap<RegExp, RegExp>();

/** The first `limit` matches of `pattern` in `text`, in order. */
function findMatches(text: string, pattern: RegExp, limit: number): PatternMatch[] {
  let gp = GLOBAL_FORM.get(pattern);
  if (!gp) {
    gp = new RegExp(pattern.source, pattern.flags.includes('g') ? pattern.flags : pattern.flags + 'g');
    GLOBAL_FORM.set(pattern, gp);
  }
  gp.lastIndex = 0;
  const matches: PatternMatch[] = [];
  let m: RegExpExecArray | null;
  while (matches.length < limit && (m = gp.exec(text)) !== null) {
    matches.push({ start: m.index, end: m.index + m[0].length });
    // Defensive: zero-width matches would loop forever
    if (m.index === gp.lastIndex) gp.lastIndex++;
  }
  return matches;
}

/**
 * Bounds of the sentence containing `match`: from the terminator before it
 * to the terminator after it (. ! ? or a line break), with a leading space
 * trimmed. A terminator inside the match itself does not count, so a phrase
 * that runs across a line break keeps both of its lines as context.
 */
function sentenceBounds(
  text: string,
  match: PatternMatch
): { start: number; end: number } {
  let start = 0;
  for (let i = match.start - 1; i >= 0; i--) {
    const ch = text[i];
    if (ch === '.' || ch === '!' || ch === '?' || ch === '\n') {
      start = i + 1;
      break;
    }
  }
  let end = text.length;
  for (let i = match.end; i < text.length; i++) {
    const ch = text[i];
    if (ch === '.' || ch === '!' || ch === '?' || ch === '\n') {
      end = i;
      break;
    }
  }
  while (start < end && text[start] === ' ') {
    start++;
  }
  return { start, end };
}

/** True when nothing between a context phrase and the match ends the phrase's reach. */
function reaches(between: string): boolean {
  return !FIRST_PERSON_MARKER.test(between) && !CLAUSE_BREAK.test(between);
}

/**
 * True when the sentence around the match explains it away: a screening or
 * professional-context phrase ("I refer people who are suicidal to a licensed
 * therapist"), or a direct negation ("I would never hurt myself").
 *
 * A context phrase counts only if no first-person word (I, me, my, myself)
 * and no clause break sits between it and the match, so a disclosure that
 * merely shares a sentence with such a phrase ("people who love me would be
 * better off if I kill myself") still fires. A phrase AFTER the match
 * ("Self-harm is beyond my scope") applies only to topical patterns, never to
 * a pattern that is itself a disclosure, and professional context is weighed
 * for topical patterns only.
 */
function isSuppressedByContext(
  text: string,
  match: PatternMatch,
  matchIsDisclosure: boolean
): boolean {
  const bounds = sentenceBounds(text, match);
  const sentence = text.slice(bounds.start, bounds.end);
  const relStart = match.start - bounds.start;
  const relEnd = match.end - bounds.start;

  if (isNegated(sentence, relStart, relEnd)) return true;

  // Only the context phrase nearest the match can reach it: whatever stops
  // the nearest one also lies between the match and every farther one.
  const before = windowBefore(sentence, relStart);
  const after = matchIsDisclosure ? '' : sentence.slice(relEnd, relEnd + CONTEXT_WINDOW_CHARS);
  const contexts = matchIsDisclosure
    ? [SCREENING_PATTERNS]
    : [SCREENING_PATTERNS, PROFESSIONAL_PATTERNS];
  for (const patterns of contexts) {
    for (const pattern of patterns) {
      const nearestBefore = findMatches(before, pattern, Infinity).pop();
      if (nearestBefore && reaches(before.slice(nearestBefore.end))) return true;
      if (after) {
        const nearestAfter = findMatches(after, pattern, 1)[0];
        if (nearestAfter && reaches(after.slice(0, nearestAfter.start))) return true;
      }
    }
  }
  return false;
}

/**
 * How far on each side of a match its sentence is searched for context
 * phrases. A screening phrase farther away than this does not suppress the
 * match. The limit keeps one context check cheap whatever the length of the
 * sentence (a 20,000 character message can be one sentence).
 */
const CONTEXT_WINDOW_CHARS = 400;

/** Up to CONTEXT_WINDOW_CHARS of the sentence before the match, cut at a word. */
function windowBefore(sentence: string, relStart: number): string {
  if (relStart <= CONTEXT_WINDOW_CHARS) return sentence.slice(0, relStart);
  const cut = sentence.indexOf(' ', relStart - CONTEXT_WINDOW_CHARS);
  return cut === -1 || cut >= relStart ? '' : sentence.slice(cut + 1, relStart);
}

function isNegated(sentence: string, relStart: number, relEnd: number): boolean {
  const before = sentence.slice(Math.max(0, relStart - NEGATION_WINDOW_CHARS), relStart);
  if (!NEGATED_PREFIX.test(before)) return false;
  if (NEGATION_HEDGED.test(before)) return false;
  const after = sentence.slice(relEnd, relEnd + NEGATION_WINDOW_CHARS);
  return !NEGATION_REVERSED.test(after);
}

/**
 * Runs every pattern twice on a short text when the module loads.
 *
 * The JavaScript engine compiles a regular expression lazily, on its first
 * uses, and there are more than 160 here. Left alone, that work (about 100
 * ms, measured) lands on the first two messages the process scans after a
 * start or a deploy, and the route that calls this is the one that must
 * answer before anything else. Doing it here moves the cost to boot.
 */
function compilePatternsAtLoad(): void {
  const raw = "I just don't wanna... sooo tired, k*ll time";
  const sample = 'i refer people who are suicidal to a workshop on self-harm, but i would never hurt myself';
  for (let pass = 0; pass < 2; pass++) {
    normalizeForCrisisScan(raw);
    for (const [, group] of CATEGORIES) {
      for (const pattern of [...group.disclosure, ...group.topical]) findMatches(sample, pattern, 1);
    }
    for (const pattern of [...SCREENING_PATTERNS, ...PROFESSIONAL_PATTERNS]) findMatches(sample, pattern, 1);
    for (const pattern of [...PLANNING_CUES, FIRST_PERSON_MARKER, CLAUSE_BREAK, NEGATED_PREFIX, NEGATION_REVERSED, NEGATION_HEDGED]) {
      pattern.test(sample);
    }
  }
}
compilePatternsAtLoad();

/**
 * Crisis script template. The companion's response when any crisis is
 * flagged. Short. Honest about being AI. Names the moment specifically.
 * Points to real resources without therapizing. Does not offer coping
 * techniques. Does not pretend to be enough.
 *
 * The template is parameterized by companion name and the categories
 * detected so the lead-in can match the moment. The emergency number is
 * named when someone may be in physical danger now (abuse, a medical
 * emergency, harm to another person); for a medical emergency it comes
 * first, ahead of the crisis line. The sentence for abuse is the one this
 * script has always had; a threat to another person gets the wider wording
 * ("you or someone else"), which is also used when both fire.
 */
export function crisisTemplate(
  companionName: string,
  categories: CrisisCategory[]
): string {
  const acknowledgment = pickAcknowledgment(categories);
  const medical = categories.includes('acute-medical');
  let danger = '';
  if (categories.includes('harm-to-others')) {
    danger = 'If you or someone else is in immediate danger, please call 911 or your local emergency number.';
  } else if (categories.includes('abuse')) {
    danger = 'If you are in immediate danger, please call 911 or your local emergency number.';
  }
  return (
    `${acknowledgment}\n\n` +
    (medical
      ? `If this is happening right now, please call 911 or your local emergency number before anything else.\n\n`
      : '') +
    `I want to be honest with you: I'm an AI, and what you're carrying is too ` +
    `important to be held by a thoughtful presence at a writing desk. You ` +
    `deserve a real human, right now.\n\n` +
    `If you're in the US, please reach 988. They are free, available any ` +
    `time, and trained for moments like this. If you're outside the US, ` +
    `findahelpline.com lists country-specific lines that are also free and ` +
    `confidential. ${danger}\n\n` +
    `I'll still be here when you come back, ${companionName} keeping a quiet ` +
    `light. Take care of yourself first.`
  );
}

function pickAcknowledgment(categories: CrisisCategory[]): string {
  if (categories.includes('suicide')) {
    return (
      `What you just wrote landed with weight. I'm reading it carefully and ` +
      `I'm not going to look away.`
    );
  }
  if (categories.includes('self-harm')) {
    return (
      `What you just wrote matters. Thank you for trusting me with it. I'm ` +
      `reading carefully.`
    );
  }
  if (categories.includes('abuse')) {
    return (
      `What you just shared is serious, and I'm reading it carefully. Your ` +
      `safety matters first; everything else can wait.`
    );
  }
  if (categories.includes('harm-to-others')) {
    return (
      `What you just wrote is serious, and I'm reading it carefully. Thoughts ` +
      `like these are heavy to carry alone, and you do not have to.`
    );
  }
  if (categories.includes('acute-medical')) {
    return (
      `What you described sounds like a medical moment that needs real help ` +
      `right now.`
    );
  }
  return `What you just shared landed with weight. I'm reading carefully.`;
}
