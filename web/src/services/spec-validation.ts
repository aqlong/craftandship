import { AppSpec, MIN_COMPLEXITY } from '../types';

export interface SpecValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

// `validateSpecForTier(spec, tier)` and `suggestTierForSpec(spec)` were removed
// 2026-05-01 with the tier system kill. Harbor sells one product; complexity is
// not gated at intake.

/**
 * Check if a spec is too simple to justify building.
 * Returns warnings only (never blocks).
 */
export function checkSpecSimplicity(spec: AppSpec): string[] {
  const warnings: string[] = [];

  const pageCount = spec.pages?.length ?? 0;
  const modelCount = spec.data_models?.length ?? 0;
  const hasAuth = spec.settings?.auth ?? false;

  if (pageCount < MIN_COMPLEXITY.minPages && modelCount < MIN_COMPLEXITY.minDataModels && !hasAuth) {
    warnings.push(
      'This website looks very simple (few pages, no data storage, no login). A lightweight site may be all you need here.'
    );
  }

  return warnings;
}

// Prohibited content keywords for Layer 2 moderation gate.
// Checked against app_name, description, page titles/purposes, and model names.
const PROHIBITED_KEYWORDS = [
  'gambling', 'casino', 'betting', 'poker', 'slot machine',
  'phishing', 'impersonation', 'fake login',
  'ponzi', 'money laundering', 'pyramid scheme',
  'counterfeit', 'fake brand', 'replica',
  'spyware', 'keylogger', 'stalking', 'doxxing',
  'drug dealer', 'drug marketplace', 'dark market',
  'weapon sales', 'firearms marketplace',
  'escort', 'adult content', 'pornography', 'pornographic',
];

// Escape a literal string for use inside a RegExp.
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Check spec fields for prohibited content keywords (Layer 2 moderation).
 * Returns an error if prohibited content is detected; the build should be blocked.
 */
export function checkProhibitedContent(spec: AppSpec): SpecValidationResult {
  const errors: string[] = [];

  // Collect all text fields to scan
  const textsToScan: string[] = [];
  if (spec.app_name) textsToScan.push(spec.app_name);
  if (spec.description) textsToScan.push(spec.description);
  for (const page of spec.pages ?? []) {
    if (page.title) textsToScan.push(page.title);
    if (page.purpose) textsToScan.push(page.purpose);
  }
  for (const model of spec.data_models ?? []) {
    if (model.name) textsToScan.push(model.name);
    for (const field of model.fields ?? []) {
      if (field.name) textsToScan.push(field.name);
    }
  }
  for (const action of spec.actions ?? []) {
    if (action.name) textsToScan.push(action.name);
    if (action.description) textsToScan.push(action.description);
  }

  // Split camelCase/PascalCase and snake/kebab identifiers into words so a
  // keyword embedded in a model/field name (e.g. "CounterfeitProduct",
  // "drug_marketplace") is still caught by the word-boundary match below,
  // while prose words like "replicate" stay intact (so "replica" does not
  // false-positive on "data replication").
  const combined = textsToScan
    .join(' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .toLowerCase();
  for (const keyword of PROHIBITED_KEYWORDS) {
    // Word-boundary match so a keyword like "replica" doesn't false-positive
    // on "replicate"/"data replication", or "betting" on "abetting".
    if (new RegExp(`\\b${escapeRegExp(keyword)}\\b`).test(combined)) {
      errors.push(
        `This app description contains prohibited content ("${keyword}"). Craft & Ship cannot build apps for illegal, fraudulent, or harmful purposes. Please contact support@craftandship.io if you believe this is an error.`
      );
      break; // One error is enough to block
    }
  }

  return { valid: errors.length === 0, errors, warnings: [] };
}
