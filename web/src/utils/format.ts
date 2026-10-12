/** Common acronyms that stay uppercase in display names */
const ACRONYMS = new Set(['cms', 'crm', 'api', 'hr', 'pos', 'erp', 'ai', 'iot']);

/**
 * Convert a hyphenated slug to a human-readable display name.
 * e.g. "neighborhood-cms" -> "Neighborhood CMS"
 */
export function displayName(slug: string): string {
  if (!slug) return '';
  return slug
    .split('-')
    .filter(Boolean)
    .map(w => ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/**
 * A URL that is safe to write into an href attribute, or "#".
 *
 * Used for values that come from the database (a build's live or preview URL
 * is written by the build worker). EJS escaping stops an attribute breakout,
 * but it does nothing about a "javascript:" or "data:" URL, which would make
 * the link run script when clicked. Only a plain http(s) URL passes.
 */
export function httpUrl(value: unknown): string {
  if (typeof value !== 'string') return '#';
  const trimmed = value.trim();
  return /^https?:\/\/[^\s"'<>`]+$/i.test(trimmed) ? trimmed : '#';
}
