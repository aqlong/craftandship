// Unit tests for the pricing FAQ list (web/src/services/pricing-faq.ts).
//
// The FAQ list is the single source of truth shared by:
//   - the visible /pricing accordion (web/views/pricing.ejs)
//   - the FAQPage JSON-LD schema (assembled by /pricing route)
// Drift between the two is the bug class these tests guard against.

import { describe, it, expect } from 'vitest';
import { pricingFaqs } from '../../src/services/pricing-faq';

describe('pricingFaqs', () => {
  const faqs = pricingFaqs({ subscription: '$39', sliding: '$19' });

  it('returns eight FAQ items', () => {
    // 2026-04-28: rebuilt to add Ocean intro + reviewer + push-back-when-engine-cant
    // Q&As. The order is stable; the count is deliberate.
    expect(faqs).toHaveLength(8);
  });

  it('every item has a non-empty question and answer', () => {
    for (const item of faqs) {
      expect(item.question).toBeTruthy();
      expect(item.question.length).toBeGreaterThan(8);
      expect(item.answer).toBeTruthy();
      expect(item.answer.length).toBeGreaterThan(20);
    }
  });

  it('interpolates the subscription + sliding prices into the price answer', () => {
    // 2026-04-28: question reframed from "How does the sliding scale work?"
    // to "How does the price work?" per founder direction. Drop the
    // "sliding scale" / "your word is enough" jargon; the price band
    // copy carries the trust gesture.
    const priceFaq = faqs.find(f => f.question.toLowerCase().includes('price'));
    expect(priceFaq).toBeDefined();
    expect(priceFaq!.answer).toContain('$39');
    expect(priceFaq!.answer).toContain('$19');
    expect(priceFaq!.answer).toContain('whatever fits your practice');
  });

  it('does NOT use defensive negation for the trust gesture', () => {
    // Per BRAND_VOICE.md 2026-04-28: avoid "we don't verify," "we don't
    // ask why," and any negation of the trust gesture. The affirmative
    // "Your word is enough" is the canonical replacement.
    for (const item of faqs) {
      expect(item.answer).not.toMatch(/we don['’]t (?:verify|ask why)/i);
      expect(item.answer).not.toMatch(/no questions asked/i);
    }
  });

  it('does NOT seed scary contingencies / shutdown talk on customer surfaces', () => {
    for (const item of faqs) {
      expect(item.answer).not.toMatch(/\bif we ever (?:close|shut)\b/i);
      expect(item.answer).not.toMatch(/\bnever held hostage\b/i);
      expect(item.answer).not.toMatch(/\bhostage\b/i);
      expect(item.answer).not.toMatch(/\block[- ]?in\b/i);
    }
  });

  it('does NOT put a calendar timetable on the conversation', () => {
    for (const item of faqs) {
      expect(item.answer).not.toMatch(/\b7[- ]?day\b|\bseven[- ]?day\b/i);
      expect(item.answer).not.toMatch(/\btrial period\b|\btrial expires\b/i);
    }
  });

  it('does NOT name competitors on customer surfaces', () => {
    // Competitor framing belongs in meta tags only (body-vs-meta SEO split).
    for (const item of faqs) {
      expect(item.answer).not.toMatch(/\b(Mindbody|Squarespace|Wix|Practice\.do|Lovable|Bolt|Webflow)\b/);
    }
  });

  it('does NOT use em dashes (BRAND_VOICE.md hard rule § 1)', () => {
    for (const item of faqs) {
      expect(item.question).not.toMatch(/\u2014|&mdash;/);
      expect(item.answer).not.toMatch(/\u2014|&mdash;/);
    }
  });

  it('respects different price params (no hardcoded numbers)', () => {
    const cheaper = pricingFaqs({ subscription: '$29', sliding: '$9' });
    const priceFaq = cheaper.find(f => f.question.toLowerCase().includes('price'));
    expect(priceFaq!.answer).toContain('$29');
    expect(priceFaq!.answer).toContain('$9');
    expect(priceFaq!.answer).not.toContain('$39');
  });

  it('order is stable (the route relies on this for the numbered accordion)', () => {
    const a = pricingFaqs({ subscription: '$39', sliding: '$19' });
    const b = pricingFaqs({ subscription: '$39', sliding: '$19' });
    expect(a.map(f => f.question)).toEqual(b.map(f => f.question));
  });
});
