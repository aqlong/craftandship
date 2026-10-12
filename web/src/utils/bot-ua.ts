// bot-ua.ts - shared User-Agent classifier for separating real human
// visitors from bots, crawlers, link-preview fetchers, and scrapers.
//
// Why this exists: the trial-cookie mint path captured no UA, so the
// trial_keepers table was ~97% non-human (link previewers, crawlers,
// uptime probes, the founder's own curl tests) with no way to tell them
// apart. Every funnel number computed off that table lied. This matcher
// lets the mint path stamp is_synthetic so funnel queries can filter to
// genuine human prospects.
//
// Two signature sets:
//   1. LINK_PREVIEW_UAS - chat/social apps that fetch a URL to build a
//      rich preview card (iMessage, Slack, Discord, WhatsApp, etc.). These
//      hit /lighthouse/table when ANY link to it is shared, minting a ghost
//      trial. Mirrors the documented magic-link previewer bypass list
//      (compose-emitted routes/auth.js, v2-magic-link-previewer-bot-bypass).
//   2. CRAWLER_UAS - search/AI crawlers, SEO bots, scrapers, headless
//      browsers, and CLI HTTP clients.
//
// Conservative by design: a real desktop/mobile browser UA (Mozilla/5.0
// ... Chrome/Safari/Firefox without a bot token) must pass through as human.

import crypto from 'crypto';

// Lowercased substrings. A UA matching any of these is treated as non-human.
const LINK_PREVIEW_UAS: readonly string[] = [
  'facebookexternalhit',
  'facebot',
  'twitterbot',
  'slackbot',
  'slack-imgproxy',
  'discordbot',
  'linkedinbot',
  'whatsapp',
  'telegrambot',
  'skypeuripreview',
  'applebot', // Apple uses this for iMessage/Spotlight link previews
  'linkpreview',
  'embedly',
  'iframely',
  'redditbot',
  'pinterestbot', // not bare 'pinterest': the Pinterest in-app browser carries it
  'vkshare',
  'snap url preview', // not bare 'snapchat': the Snapchat in-app browser carries it
  'nuzzel',
  'qwantify',
];

const CRAWLER_UAS: readonly string[] = [
  // search + AI crawlers
  'googlebot',
  'bingbot',
  'yandexbot',
  'duckduckbot',
  'baiduspider',
  'gptbot',
  'oai-searchbot',
  'chatgpt-user',
  'claudebot',
  'anthropic-ai',
  'perplexitybot',
  'ccbot',
  'google-extended',
  'bytespider',
  'amazonbot',
  // SEO / marketing crawlers
  'ahrefsbot',
  'semrushbot',
  'mj12bot',
  'dotbot',
  'petalbot',
  'dataforseo',
  'screaming frog',
  'siteauditbot',
  // generic bot / scraper / probe tokens
  'bot/',
  'spider',
  'crawler',
  'crawl',
  'scraper',
  'monitor',
  'uptimerobot',
  'pingdom',
  'statuscake',
  'headlesschrome',
  'phantomjs',
  'puppeteer',
  'playwright',
  'selenium',
  // CLI / library HTTP clients
  'curl/',
  'wget',
  'python-requests',
  'python-urllib',
  'go-http-client',
  'node-fetch',
  'axios/',
  'okhttp',
  'java/',
  'libwww-perl',
  'httpclient',
  'apache-httpclient',
  'guzzlehttp',
];

const ALL_BOT_UAS: readonly string[] = [...LINK_PREVIEW_UAS, ...CRAWLER_UAS];

/**
 * True if the User-Agent looks like a bot, crawler, link-preview fetcher,
 * scraper, or non-browser HTTP client. An empty/missing UA is treated as
 * a bot (real browsers always send one). Conservative: real browser UAs
 * (which contain "mozilla" + a browser engine token and no bot token)
 * pass through as human.
 */
export function isBotUserAgent(ua: string | undefined | null): boolean {
  if (!ua || typeof ua !== 'string') return true;
  const s = ua.toLowerCase();
  if (s.trim() === '') return true;
  for (const sig of ALL_BOT_UAS) {
    if (s.includes(sig)) return true;
  }
  return false;
}

/**
 * True specifically for a link-preview fetcher (subset of bots). Useful
 * where the handling differs (e.g. serve a minimal preview vs full page).
 */
export function isLinkPreviewBot(ua: string | undefined | null): boolean {
  if (!ua || typeof ua !== 'string') return false;
  const s = ua.toLowerCase();
  return LINK_PREVIEW_UAS.some((sig) => s.includes(sig));
}

/**
 * Short, non-reversible hash of a UA string for storage + grouping without
 * persisting the raw UA (privacy-respecting). Empty UA hashes to 'none'.
 */
export function hashUserAgent(ua: string | undefined | null): string {
  if (!ua || typeof ua !== 'string' || ua.trim() === '') return 'none';
  return crypto.createHash('sha256').update(ua).digest('hex').slice(0, 16);
}

export const _internals = { LINK_PREVIEW_UAS, CRAWLER_UAS, ALL_BOT_UAS };
