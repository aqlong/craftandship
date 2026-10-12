// Customer-visible text that used to name a reviewer, say a person builds each
// site by hand, or use the internal word for the conversation page.
//
// What the code supports: a site is built by software from a library of tested
// building blocks, the preview address is emailed when the build arrives at
// preview, and a review verdict (not a particular reviewer) must be recorded
// before the owner can make the site public. These pages used to say otherwise
// or to say it in the internal vocabulary. They read files rather than render
// routes, so they run without the app or a database.

import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const webRoot = path.join(__dirname, '../..');
const read = (rel: string): string => fs.readFileSync(path.join(webRoot, rel), 'utf-8');

// What a visitor reads or hears: text nodes plus the alt, title, aria-label and
// placeholder attribute values, with EJS tags, comments, script and style gone.
function visibleText(rel: string): string {
  const html = read(rel)
    .replace(/<%#[\s\S]*?%>/g, ' ')
    .replace(/<%[\s\S]*?%>/g, ' ')
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ');
  const attributes = [...html.matchAll(/\b(?:alt|title|aria-label|placeholder)="([^"]*)"/g)].map((m) => m[1]);
  const text = html.replace(/<[^>]+>/g, ' ');
  return `${text} ${attributes.join(' ')}`.replace(/\s+/g, ' ');
}

const RETIRED = /Senior Web Craftsman|keeper'?s table|reviews every|personally review|by hand|designs and builds|before you see the next preview/i;

describe('pages that used to name a reviewer or use the internal word', () => {
  const FILES = [
    'views/revision.ejs',
    'views/lighthouse/changes-first.ejs',
    'views/dashboard.ejs',
    'views/the-companion.ejs',
    'views/lighthouse/recall.ejs',
    'views/lighthouse/table.ejs',
  ];
  for (const file of FILES) {
    it(`${file} no longer says it`, () => {
      expect(visibleText(file)).not.toMatch(RETIRED);
    });
  }

  it('the revision page says what happens after a revision, in the order it happens', () => {
    // The worker emails the preview address when the rebuild arrives at
    // preview, and the review comes after that.
    expect(visibleText('views/revision.ejs')).toContain(
      'We rebuild it, email you the new preview address, and it will need a recorded review approval again before it goes live.',
    );
  });

  it('the page that lists review notes calls them the review\'s notes, not a person\'s', () => {
    expect(visibleText('views/lighthouse/changes-first.ejs')).toContain(
      'The review of your draft left a few small things to address',
    );
  });

  it('the dashboard describes how a site is made without naming who makes it', () => {
    expect(visibleText('views/dashboard.ejs')).toContain(
      'Your site takes shape with you in conversation, is built from a library of tested building blocks, and is kept running for you.',
    );
  });

  it('the conversation header says "your companion" under the name and keeps its id', () => {
    const view = read('views/lighthouse/table.ejs');
    expect(view).toMatch(/<span class="subtitle" id="keepers-table-eyebrow">your companion<\/span>/);
    // The header is labelled by the name and the subtitle together.
    expect(view).toContain('aria-labelledby="keepers-table-name keepers-table-eyebrow"');
  });

  it('the way back to the conversation is named for what it is', () => {
    expect(visibleText('views/lighthouse/recall.ejs')).toContain('back to your conversation with Haven');
  });

  it('the companion page describes its portrait as it is: no table in the picture', () => {
    const alt = read('views/the-companion.ejs').match(/<img[^>]*\balt="([^"]*)"/)?.[1] ?? '';
    expect(alt).toContain('lighthouse');
    expect(alt).not.toMatch(/table|keeper/i);
  });
});

describe('the web app manifest', () => {
  const manifest = JSON.parse(read('public/manifest.json')) as { description: string };
  const ogFallback = (layout: string): string => {
    const line = read(layout).split('\n').find((l) => l.includes('property="og:description"')) ?? '';
    return line.match(/metaDescription : '([^']+)'\)/)?.[1] ?? '';
  };

  it('does not name a reviewer', () => {
    expect(manifest.description).not.toMatch(/Senior Web Craftsman|by hand|personally/i);
    expect(manifest.description).toContain('Every website needs a recorded review approval before it goes live.');
  });

  it('says the same thing as the og:description fallback in both layouts', () => {
    expect(ogFallback('views/layout-harbor.ejs')).not.toBe('');
    expect(manifest.description).toBe(ogFallback('views/layout-harbor.ejs'));
    expect(manifest.description).toBe(ogFallback('views/layout.ejs'));
  });
});

describe('the intake and revision prompts and greetings', () => {
  const source = read('src/services/claude.ts');

  it('name no reviewer', () => {
    expect(source).not.toMatch(/Senior Web Craftsman/);
  });

  it('the two first greetings say the preview address is emailed and that every website needs a recorded review approval', () => {
    const greetings = source.match(/Once you approve it, we build your website and email you its preview address\. Every website needs a recorded review approval before it goes live\./g) ?? [];
    expect(greetings).toHaveLength(2);
    expect(source).not.toMatch(/personally reviews every page/);
    expect(source).not.toMatch(/1 to 2 business days/);
  });

  it('the revision prompt calls the notes the review\'s', () => {
    expect(source).toContain('The review of the build left these notes:');
  });
});
