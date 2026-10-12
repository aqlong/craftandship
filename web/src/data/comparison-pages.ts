// Competitor comparison page data + JSON-LD helpers.
//
// /vs/<competitor> landing pages target the high-intent "alternative to"
// keyword cluster. The competitive surface lives in <title>, <meta
// description>, the URL, and JSON-LD `about` references. The visible body
// copy stays affirmative: no competitor naming in the body, no defensive
// negation, no timetable phrases. See docs/BRAND_VOICE.md and the SEO
// consistency lint (scripts/check_seo_consistency.py).
//
// The brand-voice pre-commit hook (scripts/check_brand_voice.py) does not scan
// this file. The hook lists the trees it reads (web/views, web/src/services,
// web/src/routes, web/src/cron, web/src/app.ts and a few Python files), and
// web/src/data is not one of them. That is a gap, not a design: the file
// holds the fields where naming a competitor is allowed (competitorName,
// competitorUrl, pageTitle, metaDescription: the <title>, meta tags and
// JSON-LD) and also the visible body copy (tagline, lead, sections, features,
// closingPrompt) where the em-dash and no-competitor rules of
// docs/BRAND_VOICE.md apply. Adding it to the hook's list would flag the
// allowed fields, so the body fields are checked by
// tests/unit/data.comparison-pages.test.ts instead.

const SITE_ORIGIN = 'https://craftandship.io';

export interface ComparisonFeature {
  // A single feature row in the comparison table. The label names what we
  // offer; the description is the affirmative explanation.
  label: string;
  description: string;
}

export interface ComparisonPage {
  // URL slug under /vs/. Lowercase, alphanumeric.
  slug: string;
  // Display name used in title + meta + JSON-LD (NOT in visible body).
  competitorName: string;
  // Canonical URL of the named competitor (used as the JSON-LD
  // about[].url for the entity binding only; not rendered as a link).
  competitorUrl: string;
  // Page <title> shown in the browser tab + SERP snippet.
  pageTitle: string;
  // <meta name="description"> shown in SERP snippet + AI citation.
  metaDescription: string;
  // Persona descriptor for the page hero (e.g. "healers and small studios").
  audience: string;
  // One-line tagline below the hero h1.
  tagline: string;
  // Lead paragraph. Stays affirmative; addresses the persona's situation
  // without naming the competitor.
  lead: string;
  // Body sections (h2 + paragraph). Each one builds the differentiator
  // story. Affirmative throughout. Total target word count: 800 to 1500.
  sections: Array<{ heading: string; body: string }>;
  // Feature list rendered as the visible "what you get" block.
  features: ComparisonFeature[];
  // Closing prompt above the final CTA.
  closingPrompt: string;
}

const SHARED_FEATURES: ComparisonFeature[] = [
  {
    label: 'A custom website, shaped with you in conversation',
    description:
      'Your website is built from a library of tested building blocks, matched to what you told Haven. Every page is set in your voice.',
  },
  {
    label: 'A recorded review approval before it goes live',
    description:
      'Every website needs a recorded review approval before it goes live.',
  },
  {
    label: 'Hosting and care included',
    description:
      'Uptime watched day and night. We tend the lights and answer when you write. The hosting is part of the subscription.',
  },
  {
    label: 'A thoughtful AI companion who helps you find the words',
    description:
      'Haven listens during your intake conversation, remembers what matters, and writes a summary of your website in your own words. Honest about being AI.',
  },
  {
    label: 'Unlimited revisions and rebuilds',
    description:
      'Your website grows with you. Add a page, change the tone, rebuild from scratch. All across the years, all included.',
  },
  {
    label: 'Yours, kept across the years',
    description:
      'Yours, in full. The work was always yours.',
  },
];

const PAGES: Record<string, ComparisonPage> = {
  squarespace: {
    slug: 'squarespace',
    competitorName: 'Squarespace',
    competitorUrl: 'https://www.squarespace.com',
    pageTitle: 'A Squarespace alternative for healers and small studios',
    metaDescription:
      'Looking for a Squarespace alternative? Craft & Ship makes custom websites with healers, therapists, breathwork facilitators, and event producers, shaped with an AI companion, built from tested building blocks, and needing a recorded review approval before going live. Hosting and care included from $19 to $39 a month.',
    audience: 'healers, therapists, breathwork facilitators, and small studios',
    tagline: 'A custom website, shaped with you in conversation.',
    lead:
      'If you are a healer, therapist, breathwork facilitator, or small studio looking for a real website that fits your work, not another template you wrestle into shape, this is the place. Every Craft & Ship website begins with Haven, an AI companion who listens during your intake conversation and writes a summary of your website in your own words. That summary becomes your website, built from a library of tested building blocks, and it needs a recorded review approval before it goes live.',
    sections: [
      {
        heading: 'Designed for the way practitioners actually work.',
        body:
          'Practitioners do not run their work from a settings panel. They run it from a phone call, a session, a payment app, a calendar. The website needs to fit around all of that, not the other way around. We start with a long, unhurried conversation about your work: your offerings, your clients, the rhythm of your week, the words you would use if a friend asked you over coffee. From that, we build a site that reads like you wrote it, because, in a real sense, you did.',
      },
      {
        heading: 'Tested building blocks. A recorded review approval before it goes live.',
        body:
          'Your website is built from a library of tested building blocks, matched to what you told Haven. Every website needs a recorded review approval before it goes live.',
      },
      {
        heading: 'Yours. Kept across the years.',
        body:
          'Your website is yours. Hosting and care are included; we tend the lights and answer when you write. Add a page, change the tone, rebuild the whole thing from scratch when the work shifts. All the revisions and rebuilds you need, kept across the years. Yours, in full.',
      },
      {
        heading: 'A companion who is honest about being AI.',
        body:
          'Haven is a thoughtful AI companion who helps you find the words during the intake conversation. She listens, remembers, and is honest about being AI. She is not a marketing ghost, not a sales agent, not a chatbot for the sake of being a chatbot. She is part of every Craft & Ship subscription, and she stays available whenever you need her.',
      },
      {
        heading: 'One subscription, sliding to fit your practice.',
        body:
          'Choose any amount from $39 down to $19 a month, whatever fits your practice. One subscription covers the website, the hosting, the care, the companion, and unlimited revisions and rebuilds. One number.',
      },
    ],
    features: SHARED_FEATURES,
    closingPrompt:
      'When you are ready, begin a conversation with Haven. Talk for as long as it feels right. When the shape of your website is clear, you decide.',
  },

  mindbody: {
    slug: 'mindbody',
    competitorName: 'Mindbody',
    competitorUrl: 'https://www.mindbodyonline.com',
    pageTitle: 'A Mindbody alternative for wellness practitioners and studios',
    metaDescription:
      'Looking for a Mindbody alternative? Craft & Ship is a custom website shaped with you in conversation and built from a library of tested building blocks, with hosting and care included from $19 to $39 a month. Yours, kept across the years.',
    audience: 'wellness practitioners and small studios',
    tagline: 'A website that fits your work, kept across the years.',
    lead:
      'If you run a wellness practice, a yoga studio, a sound-healing space, or a small therapy clinic and you want a website that belongs to you, this is the place. Craft & Ship is a studio that makes custom websites with practitioners, then keeps the lights on. Hosting and care are included. Unlimited revisions and rebuilds across the years.',
    sections: [
      {
        heading: 'Built around your practice, not a database of studios.',
        body:
          'Wellness practitioners run their work in their own rhythm: a class on Tuesday morning, a one-on-one on Thursday evening, a workshop one Saturday a quarter. The website should follow your week, not flatten it into a generic studio template. We build a custom website with you, in your voice. Booking, scheduling, intake forms, classes, packages, reviews. All shaped to your work.',
      },
      {
        heading: 'Yours. Kept across the years.',
        body:
          'Your website is yours. The client list, the offerings, the writing, the design. If your practice changes shape, we rebuild with you. Yours, in full. The work was always yours.',
      },
      {
        heading: 'A recorded review approval before it goes live.',
        body:
          'Your website is built from a library of tested building blocks, matched to what you told Haven. Every website needs a recorded review approval before it goes live.',
      },
      {
        heading: 'A companion who listens during intake.',
        body:
          'Haven is a thoughtful AI companion who helps you find the words during the intake conversation. She listens, remembers what matters about your practice, and writes a summary of your website in your own words. Honest about being AI. Part of every Craft & Ship subscription.',
      },
      {
        heading: 'One subscription, sliding to fit your practice.',
        body:
          'Choose any amount from $39 down to $19 a month, whatever fits your practice. One subscription covers the website, the hosting, the care, the companion, and unlimited revisions and rebuilds. One number, sliding to fit.',
      },
    ],
    features: SHARED_FEATURES,
    closingPrompt:
      'When you are ready, begin a conversation with Haven. Tell her about your practice. Talk for as long as it feels right.',
  },

  wix: {
    slug: 'wix',
    competitorName: 'Wix',
    competitorUrl: 'https://www.wix.com',
    pageTitle: 'A Wix alternative for practitioners and small studios',
    metaDescription:
      'Looking for a Wix alternative? Craft & Ship makes custom websites with practitioners, built from a library of tested building blocks and needing a recorded review approval before they go live. Hosting and care included from $19 to $39 a month.',
    audience: 'practitioners and small studios',
    tagline: 'A real website, with a recorded review approval before it goes live.',
    lead:
      'If you have outgrown a builder app and you want a website that reads like you wrote it, this is the place. Craft & Ship builds every site with the practitioner whose name is on it, in conversation with Haven. Every website needs a recorded review approval before it goes live.',
    sections: [
      {
        heading: 'No template wrestling. Real design, in your voice.',
        body:
          'You have a story. The website should sound like the person telling it. We start with a long conversation about your work, then build a site from a library of tested building blocks. The result fits your voice, your aesthetic, your offerings. Not the same fifty templates everyone else picks from.',
      },
      {
        heading: 'Tested building blocks. A recorded review approval before it goes live.',
        body:
          'Every website is built from a library of tested building blocks and needs a recorded review approval before it goes live.',
      },
      {
        heading: 'Hosting, care, and unlimited revisions, all included.',
        body:
          'Your website is hosted with us. We watch uptime day and night, fix things when they break, and answer when you write. Add a page, change the tone, rebuild the whole thing from scratch. All the revisions and rebuilds you need, kept across the years.',
      },
      {
        heading: 'A companion who is honest about being AI.',
        body:
          'Haven helps you find the words during your intake conversation. She listens, remembers, and is honest about being AI. She is part of every Craft & Ship subscription, available whenever you need her.',
      },
      {
        heading: 'One subscription, sliding to fit your practice.',
        body:
          'Choose any amount from $39 down to $19 a month, whatever fits your practice. One subscription covers everything. One number.',
      },
    ],
    features: SHARED_FEATURES,
    closingPrompt:
      'When you are ready, begin a conversation. We will read every word.',
  },

  acuity: {
    slug: 'acuity',
    competitorName: 'Acuity',
    competitorUrl: 'https://acuityscheduling.com',
    pageTitle: 'An Acuity alternative for healers, therapists, and studios',
    metaDescription:
      'Looking for an Acuity alternative that includes a real website, not just a booking page? Craft & Ship makes custom websites with practitioners, built from a library of tested building blocks and needing a recorded review approval before they go live, with hosting and care included from $19 to $39 a month.',
    audience: 'healers, somatic therapists, and small studios',
    tagline: 'A website that fits your work, with the booking woven in.',
    lead:
      'If you run a healing practice, a somatic-therapy studio, or a small wellness space and you want a website that tells the full story of your work, holds your offerings, and stays kept across the years, this is the place. Craft & Ship makes custom websites with healers and therapists.',
    sections: [
      {
        heading: 'A website that holds the whole story of your work.',
        body:
          'Booking matters. So does the rest. The story of your work, the offerings, the testimonials, the writing that brings someone from "I have a problem" to "I think I should reach out." We build the whole thing as one website: home page, offerings, intake form, booking flow, payment. All in your voice, all shaped with you.',
      },
      {
        heading: 'A recorded review approval before it goes live.',
        body:
          'Your website is built from a library of tested building blocks, matched to what you told Haven. Every website needs a recorded review approval before it goes live.',
      },
      {
        heading: 'Hosting and care included. Unlimited revisions.',
        body:
          'Your website is hosted with us. We watch uptime, fix things when they break, and answer when you write. Add a page, change the tone, rebuild the whole thing. All across the years, all included.',
      },
      {
        heading: 'A companion who listens during intake.',
        body:
          'Haven helps you find the words during the intake conversation. She listens, remembers what matters about your practice, and writes a summary of your website in your own words. Honest about being AI. Part of every subscription.',
      },
      {
        heading: 'One subscription, sliding to fit your practice.',
        body:
          'Choose any amount from $39 down to $19 a month, whatever fits your practice. The subscription covers the website, the hosting, the care, the companion, the revisions, the rebuilds. One number.',
      },
    ],
    features: SHARED_FEATURES,
    closingPrompt:
      'When you are ready, begin a conversation with Haven. Tell her about your practice and the people you serve.',
  },
};

export function getComparisonPage(slug: string): ComparisonPage | null {
  if (!slug || typeof slug !== 'string') return null;
  return PAGES[slug.toLowerCase()] || null;
}

export function listComparisonPages(): ComparisonPage[] {
  return Object.values(PAGES);
}

// JSON-LD article schema for a comparison page. Uses Article + about[]
// referencing both Craft & Ship and the named competitor as Organization
// entities. Schema.org has no canonical "ComparisonPage" type; Article
// with linked entities is what AI search engines (ChatGPT, Claude,
// Perplexity, Google AI Overviews) actually index for citation.
export function comparisonPageJsonLd(page: ComparisonPage): Record<string, unknown> {
  return {
    '@context': 'https://schema.org',
    '@type': 'Article',
    '@id': `${SITE_ORIGIN}/vs/${page.slug}#article`,
    headline: page.pageTitle,
    description: page.metaDescription,
    url: `${SITE_ORIGIN}/vs/${page.slug}`,
    inLanguage: 'en',
    isPartOf: { '@id': `${SITE_ORIGIN}#organization` },
    author: { '@id': `${SITE_ORIGIN}/about/ocean#person` },
    publisher: { '@id': `${SITE_ORIGIN}#organization` },
    about: [
      { '@id': `${SITE_ORIGIN}#organization` },
      {
        '@type': 'Organization',
        name: page.competitorName,
        url: page.competitorUrl,
      },
    ],
  };
}

// JSON-LD CollectionPage schema for the /vs hub. Lists every comparison
// page as ItemList entries so AI engines can crawl from the hub.
export function comparisonHubJsonLd(): Record<string, unknown> {
  const items = listComparisonPages().map((page, idx) => ({
    '@type': 'ListItem',
    position: idx + 1,
    url: `${SITE_ORIGIN}/vs/${page.slug}`,
    name: page.pageTitle,
  }));
  return {
    '@context': 'https://schema.org',
    '@type': 'CollectionPage',
    '@id': `${SITE_ORIGIN}/vs#collection`,
    url: `${SITE_ORIGIN}/vs`,
    name: 'Craft & Ship: a real alternative to template website builders',
    description:
      'Craft & Ship makes custom websites with healers, therapists, breathwork facilitators, and small studios. Built from a library of tested building blocks, with a recorded review approval before it goes live, kept across the years.',
    isPartOf: { '@id': `${SITE_ORIGIN}#organization` },
    mainEntity: {
      '@type': 'ItemList',
      itemListElement: items,
    },
  };
}
