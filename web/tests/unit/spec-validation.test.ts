import { describe, it, expect } from 'vitest';
import { checkSpecSimplicity, checkProhibitedContent } from '../../src/services/spec-validation';
import { AppSpec } from '../../src/types';

function makeSpec(overrides: Partial<AppSpec> = {}): AppSpec {
  return {
    spec_version: '0.1',
    app_name: 'test-app',
    description: 'A test app',
    user_roles: [
      { role: 'user', description: 'Regular user', can: ['view'] },
    ],
    pages: [
      { path: '/', title: 'Home', purpose: 'Landing', auth_required: false, allowed_roles: [] },
      { path: '/dashboard', title: 'Dashboard', purpose: 'Main view', auth_required: true, allowed_roles: ['user'] },
    ],
    data_models: [
      { name: 'Item', fields: [{ name: 'title', type: 'text', required: true }], belongs_to: null, has_many: [] },
    ],
    actions: [],
    settings: { auth: true, seed_data: true },
    ...overrides,
  };
}

// `validateSpecForTier` (and the 6 cases that exercised it) was removed
// 2026-05-01 with the tier system kill. Harbor sells one product; complexity
// is not gated at intake.

describe('checkSpecSimplicity', () => {
  it('warns for very simple spec (1 page, no models, no auth)', () => {
    const simple = makeSpec({
      pages: [{ path: '/', title: 'Home', purpose: 'Landing', auth_required: false, allowed_roles: [] }],
      data_models: [],
      settings: { auth: false, seed_data: false },
    });
    const warnings = checkSpecSimplicity(simple);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toMatch(/very simple/);
  });

  it('does not warn when spec has auth', () => {
    const spec = makeSpec({
      pages: [{ path: '/', title: 'Home', purpose: 'Landing', auth_required: false, allowed_roles: [] }],
      data_models: [],
      settings: { auth: true, seed_data: false },
    });
    const warnings = checkSpecSimplicity(spec);
    expect(warnings).toHaveLength(0);
  });

  it('does not warn when spec has enough pages and models', () => {
    const warnings = checkSpecSimplicity(makeSpec());
    expect(warnings).toHaveLength(0);
  });
});

// `suggestTierForSpec` (and the 4 cases that exercised it) was removed
// 2026-05-01 with the tier system kill.

describe('checkProhibitedContent', () => {
  it('passes for a normal business app', () => {
    const result = checkProhibitedContent(makeSpec({
      app_name: 'bakery-orders',
      description: 'Order management for a local bakery',
    }));
    expect(result.valid).toBe(true);
    expect(result.errors).toHaveLength(0);
  });

  it('blocks app with gambling keyword in description', () => {
    const result = checkProhibitedContent(makeSpec({
      description: 'Online casino and betting platform',
    }));
    expect(result.valid).toBe(false);
    expect(result.errors[0]).toContain('prohibited content');
  });

  it('blocks app with prohibited keyword in app_name', () => {
    const result = checkProhibitedContent(makeSpec({
      app_name: 'phishing-kit-manager',
    }));
    expect(result.valid).toBe(false);
    expect(result.errors[0]).toContain('prohibited content');
  });

  it('blocks app with prohibited keyword in page title', () => {
    const result = checkProhibitedContent(makeSpec({
      pages: [
        { path: '/drugs', title: 'Drug Marketplace', purpose: 'Browse listings', auth_required: true, allowed_roles: ['user'] },
      ],
    }));
    expect(result.valid).toBe(false);
  });

  it('blocks app with prohibited keyword in model name', () => {
    const result = checkProhibitedContent(makeSpec({
      data_models: [
        { name: 'CounterfeitProduct', fields: [{ name: 'brand', type: 'text', required: true }], belongs_to: null, has_many: [] },
      ],
    }));
    expect(result.valid).toBe(false);
  });

  it('blocks app with prohibited keyword in action description', () => {
    const result = checkProhibitedContent(makeSpec({
      actions: [
        { name: 'track', description: 'Stalking target location', trigger: 'button_click', role: 'user', steps: [] },
      ],
    }));
    expect(result.valid).toBe(false);
  });

  it('is case-insensitive', () => {
    const result = checkProhibitedContent(makeSpec({
      description: 'A GAMBLING app for BETTING',
    }));
    expect(result.valid).toBe(false);
  });

  it('does not false-positive on legitimate words containing a keyword substring', () => {
    // "replica" must not block "data replication"/"replicate"; "betting"
    // must not block "abetting"; "escort" must not block "escorted".
    const result = checkProhibitedContent(makeSpec({
      description: 'A tool to replicate escorted, guided data replication backups.',
      data_models: [
        { name: 'ReplicationJob', fields: [{ name: 'name', type: 'text', required: true }], belongs_to: null, has_many: [] },
      ],
    }));
    expect(result.valid).toBe(true);
  });

  it('still catches a keyword embedded in a compound identifier', () => {
    // Word-boundary matching must not let "CounterfeitProduct" /
    // "drug_marketplace" slip through: identifiers are split into words first.
    const result = checkProhibitedContent(makeSpec({
      data_models: [
        { name: 'CounterfeitProduct', fields: [{ name: 'drug_marketplace_id', type: 'text', required: true }], belongs_to: null, has_many: [] },
      ],
    }));
    expect(result.valid).toBe(false);
  });
});
