// Single source of truth for the /pricing FAQ block. The same list feeds:
//  1. the visible HTML accordion in `web/views/pricing.ejs`, and
//  2. the FAQPage JSON-LD schema served via the layout's pageJsonLd slot.
//
// Keeping both in lockstep is the whole point, if visible copy and the
// schema drift, AI engines cite stale Q/A pairs that don't appear on the
// page (an embarrassment + a citation-credibility hit). Tests assert
// every visible question + answer matches the schema.
//
// All copy follows BRAND_VOICE.md: affirmative, no shutdown talk, no
// "we don't verify" / "we don't ask why" negation of the trust gesture,
// no competitor framing, no calendar timetable on the conversation.

import type { FaqItem } from './structured-data';

export interface PricingFaqDeps {
  // The headline subscription price (e.g. "$39"). Pulled from res.locals
  // so the FAQ stays consistent with the hero + card + nav.
  subscription: string;
  // The lower end of the price band (e.g. "$19"). Same source.
  sliding: string;
}

export function pricingFaqs(deps: PricingFaqDeps): FaqItem[] {
  const { subscription, sliding } = deps;
  return [
    {
      question: 'How does the price work?',
      answer: `Choose any amount from ${subscription} down to ${sliding} a month, whatever fits your practice. There is one subscription. You decide what to pay inside that band.`,
    },
    {
      question: 'Who is Ocean?',
      answer:
        'Ocean Longnion is a Senior Web Craftsman and the founder of Craft & Ship.',
    },
    {
      question: 'How is each website reviewed?',
      answer:
        'Every website needs a recorded review approval before it goes live. Your site is built from a library of tested building blocks, matched to what you told Haven, and the review is the step between the finished preview and the public web.',
    },
    {
      question: 'What if my website needs grow over time?',
      answer:
        'Revisions and rebuilds are unlimited across the years. As your practice changes, your site changes with it. The subscription stays the same.',
    },
    {
      question: 'What if I want something the engine cannot build?',
      answer:
        'Most websites for healers, therapists, and event producers fit the building blocks we have. Anything the building blocks do not cover is not written as custom code. No surprises and no smoke.',
    },
    {
      question: 'How does billing work?',
      answer:
        'A monthly subscription, billed monthly. The relationship is the contract; the rest is paperwork. You can cancel it any time, and write to us if you would rather pause.',
    },
    {
      question: 'What if I want to leave?',
      answer:
        'Your website is yours. Take it to your own hosting, to another developer, or wherever you like. The subscription pauses the day you ask.',
    },
    {
      question: 'Can a few free seats happen?',
      answer:
        'Yes, quietly. A small grant program runs each month for practitioners doing real work in communities that can carry the cost less easily. If that fits, write to Ocean directly.',
    },
  ];
}
