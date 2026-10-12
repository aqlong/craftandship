// Unit tests for getKnowledgeContentHash() - the SHA-256 fingerprint of the
// support chat knowledge base.
//
// The hash is the trigger for invalidating cached support_kb answers on
// startup (see web/src/server.ts refreshSupportKbIfStale). It MUST be stable
// when nothing material has changed and MUST change when any of the dynamic
// inputs (pricing, tier copy, etc.) shift, otherwise the cache will serve
// stale answers across deploys.

import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  generateKnowledge,
  getKnowledgeContentHash,
} from '../../src/services/support-knowledge';

describe('getKnowledgeContentHash', () => {
  it('returns a 64-character hex SHA-256 digest', () => {
    const hash = getKnowledgeContentHash();
    expect(hash).toMatch(/^[a-f0-9]{64}$/);
  });

  it('is deterministic across repeated calls in the same process', () => {
    const a = getKnowledgeContentHash();
    const b = getKnowledgeContentHash();
    expect(a).toBe(b);
  });

  it('matches the SHA-256 digest of generateKnowledge() output', async () => {
    const { createHash } = await import('crypto');
    const expected = createHash('sha256')
      .update(generateKnowledge(), 'utf8')
      .digest('hex');
    expect(getKnowledgeContentHash()).toBe(expected);
  });

  it('changes when the rendered knowledge changes', async () => {
    const baseline = getKnowledgeContentHash();
    // Mutate the FEATURE_DISPLAY_PRICES constants module that generateKnowledge()
    // imports from. We do not touch source on disk; we patch the live binding
    // so the next render picks up a new price string. (Was TIER_DISPLAY_PRICES
    // before the 2026-05-01 tier kill; FEATURE_DISPLAY_PRICES is the remaining
    // mutable price source still surfaced in the support knowledge.)
    const types = await import('../../src/types');
    const priceMap = types.FEATURE_DISPLAY_PRICES as Record<string, string>;
    const original = priceMap.small;
    try {
      priceMap.small = '$1';
      const mutated = getKnowledgeContentHash();
      expect(mutated).not.toBe(baseline);
    } finally {
      priceMap.small = original;
    }
    // Restore: hash should match the original baseline once the mutation is
    // undone, otherwise the test left the module in a dirty state for any
    // sibling test.
    expect(getKnowledgeContentHash()).toBe(baseline);
  });
});

describe('generateKnowledge: the preview', () => {
  it('walks the customer through a preview and does not call it private', () => {
    const text = generateKnowledge();
    expect(text).toContain('4. Walk around your preview');
    expect(text).not.toMatch(/private\s+preview/i);
  });
});

describe('generateKnowledge: who builds and who reviews', () => {
  // The support chat answers customers from this text. The site is assembled
  // from a library of tested building blocks; every website needs a recorded
  // review approval before it goes live; and the worker emails the preview
  // address when the build arrives at preview, so the review does not come
  // before it.
  const text = generateKnowledge();

  it('says the two settled things', () => {
    expect(text).toContain('built from a library of tested building blocks');
    expect(text).toContain('Every website needs a recorded review approval before it goes live');
  });

  it('promises no review turnaround and no reply time', () => {
    expect(text).not.toMatch(/business day|within a few hours|response time|reviewed within|1 to 2 /i);
  });

  it('names no builder and no personal reviewer, and puts no review before the preview address', () => {
    expect(text).not.toMatch(/designed and built with you by|Ocean \(a Senior Web Craftsman\)|Senior Web Craftsman reviews|personally|by hand/i);
    expect(text).not.toMatch(/so you and a Senior Web Craftsman can review it before it goes public/);
    expect(text).toContain('we email you its preview address and it enters "preview" status while it needs a recorded review approval before it goes public');
  });

  it('the Python mirror used by the coordinator says the same', () => {
    // coordinator/support_knowledge.py joins its string literals; undo the joins.
    const mirror = fs
      .readFileSync(path.join(__dirname, '..', '..', '..', 'coordinator', 'support_knowledge.py'), 'utf-8')
      .replace(/"\s*\n\s*"/g, '');
    expect(mirror).toContain('built from a library of tested building blocks');
    expect(mirror).toContain('Every website needs a recorded review approval before it goes live');
    expect(mirror).toContain('we email you its preview address and it enters \\"preview\\" status while it needs a recorded review approval before it goes public');
    expect(mirror).not.toMatch(/senior engineer|designs and builds|designed and built with you by/i);
  });
});
