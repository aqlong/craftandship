// Structured data (JSON-LD) helpers for SEO + AI search optimization.
//
// schema.org JSON-LD is a foundation move for AI search engines (ChatGPT,
// Claude, Perplexity, Google AI Overviews). Every public page should ship Organization + Person (Ocean
// as author entity); pages with FAQ-shaped content should additionally ship
// FAQPage so AI engines can extract Q/A pairs cleanly.
//
// All helpers return a JSON-encodable object. The view layer wraps it in a
// <script type="application/ld+json"> tag (see web/views/layout-harbor.ejs).
//
// NOTE: affirmative voice rule (BRAND_VOICE.md § 2 / 2026-04-28 extension):
// every string in this file is part of the customer surface AI engines will
// paraphrase into citations. Keep it forward-looking. No competitor names.
// No "if we ever close" / "won't lock in" / "never held hostage" phrasings.
// No timetable phrases like "7 days" or "trial period."

const SITE_ORIGIN = 'https://craftandship.io';

// JSON-LD nodes have an `@type` in canonical form. The `@graph` wrapper is
// the one exception: it groups multiple typed nodes and has no top-level
// `@type` of its own, so we accept either shape.
export interface JsonLdNode {
  '@context'?: string | Record<string, string>;
  '@type'?: string | string[];
  [key: string]: unknown;
}

// Organization schema for Craft & Ship. Reused on every page that ships JSON-LD.
// `founder` is a nested Person rather than a separate top-level node so the
// graph stays tight and AI-engine extraction is unambiguous.
//
// `sameAs` is intentionally omitted when no profile URLs are confirmed yet.
// Schema.org rich-results validators flag empty arrays as "empty value." We
// only emit the key once `oceanSameAs` / org sameAs URLs are populated via
// env or future config.
export function organizationJsonLd(): JsonLdNode {
  const node: JsonLdNode = {
    '@context': 'https://schema.org',
    '@type': 'Organization',
    '@id': `${SITE_ORIGIN}#organization`,
    name: 'Craft & Ship',
    alternateName: ['CraftAndShip', 'Craft and Ship'],
    url: SITE_ORIGIN,
    logo: `${SITE_ORIGIN}/brand-harbor/favicon.svg`,
    description:
      'Craft & Ship makes custom websites for healers, therapists, breathwork facilitators, event producers, and the practitioners who run their own practices. Every website takes shape in a conversation with Haven, a thoughtful AI companion, is built from a library of tested building blocks, and needs a recorded review approval before it goes live.',
    founder: { '@id': `${SITE_ORIGIN}/about/ocean#person` },
    foundingLocation: 'Austin, Texas, United States',
    knowsAbout: [
      'custom website design',
      'website building for healers',
      'website building for therapists',
      'website building for somatic practitioners',
      'website building for breathwork facilitators',
      'website building for event producers',
      'AI co-creation companion',
      'reviewed website builds',
    ],
  };
  const sameAs = collectOrganizationSameAs();
  if (sameAs.length > 0) node.sameAs = sameAs;
  return node;
}

// Collect Organization sameAs URLs from env. When CAS_ORG_SAMEAS is set
// (comma-separated URL list), each valid URL is emitted in the schema.
// AI engines weight verified-identity edges; populate this once the brand's
// public profile URLs (LinkedIn page, X handle, Instagram, etc.) are confirmed.
export function collectOrganizationSameAs(): string[] {
  const raw = process.env.CAS_ORG_SAMEAS;
  if (!raw) return [];
  return parseSameAsList(raw);
}

// Person schema for Ocean Longnion, the Senior Web Craftsman behind Craft & Ship.
// Used on /about and /about/ocean. The `@id` is canonical and stable; other
// schema nodes (Organization.founder, Article.author) reference it by @id.
//
// `sameAs` is intentionally omitted when no profile URLs are confirmed yet.
// Schema.org rich-results validators flag empty arrays as "empty value."
// Populate via the CAS_OCEAN_SAMEAS env var (comma-separated URL list).
export function oceanPersonJsonLd(): JsonLdNode {
  const node: JsonLdNode = {
    '@context': 'https://schema.org',
    '@type': 'Person',
    '@id': `${SITE_ORIGIN}/about/ocean#person`,
    name: 'Ocean Longnion',
    jobTitle: 'Senior Web Craftsman',
    description:
      'Senior Web Craftsman and founder of Craft & Ship, which makes custom websites for healers, therapists, and event producers.',
    url: `${SITE_ORIGIN}/about/ocean`,
    worksFor: { '@id': `${SITE_ORIGIN}#organization` },
    knowsAbout: [
      'web application architecture',
      'TypeScript',
      'PostgreSQL',
      'website craft for soulful practitioners',
      'AI honesty rails',
    ],
  };
  const sameAs = collectOceanSameAs();
  if (sameAs.length > 0) node.sameAs = sameAs;
  return node;
}

// Collect Ocean's sameAs URLs from env. CAS_OCEAN_SAMEAS is a comma-separated
// list (LinkedIn, GitHub, personal site, etc.). Setting this is the single
// most-impactful Phase 0 follow-up: author-entity verification is what AI
// search engines weight most heavily for citation eligibility.
export function collectOceanSameAs(): string[] {
  const raw = process.env.CAS_OCEAN_SAMEAS;
  if (!raw) return [];
  return parseSameAsList(raw);
}

// Pure helper: parse a comma-separated env string into a clean URL list.
// Drops empty / whitespace-only entries. Validates each entry parses as a
// URL with a hostname (rejects garbage like "not-a-url"). Trims surrounding
// whitespace. Stable order matches input order.
export function parseSameAsList(raw: string): string[] {
  return raw
    .split(',')
    .map(s => s.trim())
    .filter(s => s.length > 0)
    .filter(s => {
      try {
        const u = new URL(s);
        return Boolean(u.hostname);
      } catch {
        return false;
      }
    });
}

export interface FaqItem {
  question: string;
  answer: string;
}

// FAQPage schema. Pages with FAQ-shaped content (currently /pricing) emit
// this so AI engines can extract Q/A pairs and cite individual answers.
// The `mainEntity` is an array of Question nodes, each with an
// acceptedAnswer Answer node.
//
// Caller passes the same FAQ list the EJS view renders, ensuring schema and
// visible copy never drift apart. (Tests assert both.)
export function faqPageJsonLd(items: FaqItem[]): JsonLdNode {
  return {
    '@context': 'https://schema.org',
    '@type': 'FAQPage',
    mainEntity: items.map((item) => ({
      '@type': 'Question',
      name: item.question,
      acceptedAnswer: {
        '@type': 'Answer',
        text: item.answer,
      },
    })),
  };
}

// Compose multiple JSON-LD nodes into one @graph wrapper. AI engines read
// either form; @graph is cleaner when a page ships more than one node
// (e.g. /about/ocean ships Organization + Person + breadcrumbs).
export function jsonLdGraph(nodes: JsonLdNode[]): JsonLdNode {
  return {
    '@context': 'https://schema.org',
    '@graph': nodes.map((node) => {
      const { '@context': _ctx, ...rest } = node;
      return rest as JsonLdNode;
    }),
  };
}

// Render a JSON-LD node as a <script type="application/ld+json"> string. The
// caller embeds the returned string in the layout through <%- %> (raw), so
// this function owns the escaping.
//
// JSON.stringify output is not safe inside a script element as it stands: the
// HTML parser ends the element at the first "</script" (in any letter case,
// and whatever follows it), and "<!--" followed by "<script" puts the parser
// into a state that swallows the real end tag. Rather than pattern-match those
// sequences, every "<" is written as the JSON escape \u003c (and ">" as
// \u003e). No tag or comment can then start inside the element at all, and a
// JSON parser reads the escapes back as the original characters, so consumers
// of the structured data see identical values. "&" is left alone: character
// references are not decoded inside a script element. U+2028 and U+2029 are
// escaped too: they are legal in JSON but were line terminators in JavaScript
// before ES2019.
//
// `nonce`: when supplied, the tag carries `nonce="<nonce>"` so it stays valid
// under the nonce-based CSP. CSP3 exempts most JSON-LD blocks from script-src
// (browsers treat them as data), so this is defense in depth. The value comes
// from app.ts (base64 of random bytes); anything outside the base64 alphabet
// is dropped so the attribute cannot be broken out of.
const JSON_LD_ESCAPES: Record<string, string> = {
  '<': '\\u003c',
  '>': '\\u003e',
  '\u2028': '\\u2028',
  '\u2029': '\\u2029',
};

export function escapeJsonForScript(json: string): string {
  return json.replace(/[<>\u2028\u2029]/g, (ch) => JSON_LD_ESCAPES[ch]);
}

export function jsonLdScriptTag(node: JsonLdNode, nonce?: string): string {
  const json = escapeJsonForScript(JSON.stringify(node));
  const safeNonce = nonce ? nonce.replace(/[^A-Za-z0-9+/=_-]/g, '') : '';
  const nonceAttr = safeNonce ? ` nonce="${safeNonce}"` : '';
  return `<script type="application/ld+json"${nonceAttr}>${json}</script>`;
}
