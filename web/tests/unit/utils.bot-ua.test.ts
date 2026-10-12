import { describe, it, expect } from 'vitest';
import {
  isBotUserAgent,
  isLinkPreviewBot,
  hashUserAgent,
  _internals,
} from '../../src/utils/bot-ua';

describe('isBotUserAgent', () => {
  it('treats missing/empty UA as a bot (real browsers always send one)', () => {
    expect(isBotUserAgent(undefined)).toBe(true);
    expect(isBotUserAgent(null)).toBe(true);
    expect(isBotUserAgent('')).toBe(true);
    expect(isBotUserAgent('   ')).toBe(true);
  });

  it('flags link-preview fetchers (the main trial-mint contaminant)', () => {
    expect(isBotUserAgent('facebookexternalhit/1.1')).toBe(true);
    expect(isBotUserAgent('Mozilla/5.0 (compatible; Discordbot/2.0)')).toBe(true);
    expect(isBotUserAgent('Slackbot-LinkExpanding 1.0')).toBe(true);
    expect(isBotUserAgent('WhatsApp/2.23')).toBe(true);
    expect(isBotUserAgent('TelegramBot (like TwitterBot)')).toBe(true);
    expect(isBotUserAgent('Applebot/0.1')).toBe(true);
    expect(isBotUserAgent('LinkedInBot/1.0')).toBe(true);
  });

  it('flags search/AI crawlers + SEO bots', () => {
    expect(isBotUserAgent('Mozilla/5.0 (compatible; Googlebot/2.1)')).toBe(true);
    expect(isBotUserAgent('GPTBot/1.0')).toBe(true);
    expect(isBotUserAgent('ClaudeBot/1.0')).toBe(true);
    expect(isBotUserAgent('Mozilla/5.0 (compatible; AhrefsBot/7.0)')).toBe(true);
    expect(isBotUserAgent('Mozilla/5.0 (compatible; bingbot/2.0)')).toBe(true);
  });

  it('flags CLI / library HTTP clients (founder curl tests, scripts)', () => {
    expect(isBotUserAgent('curl/8.4.0')).toBe(true);
    expect(isBotUserAgent('Wget/1.21')).toBe(true);
    expect(isBotUserAgent('python-requests/2.31.0')).toBe(true);
    expect(isBotUserAgent('Go-http-client/1.1')).toBe(true);
    expect(isBotUserAgent('node-fetch/1.0')).toBe(true);
  });

  it('flags headless/automation browsers', () => {
    expect(isBotUserAgent('Mozilla/5.0 HeadlessChrome/120.0')).toBe(true);
    expect(isBotUserAgent('Mozilla/5.0 ... PhantomJS/2.1')).toBe(true);
  });

  it('lets real desktop + mobile browser UAs through as human', () => {
    const realUAs = [
      // Chrome desktop
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      // Safari iPhone
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
      // Firefox
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
      // Android Chrome
      'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
    ];
    for (const ua of realUAs) {
      expect(isBotUserAgent(ua), `should be human: ${ua}`).toBe(false);
    }
  });
});

describe('isLinkPreviewBot', () => {
  it('matches only link-preview fetchers, not generic crawlers', () => {
    expect(isLinkPreviewBot('facebookexternalhit/1.1')).toBe(true);
    expect(isLinkPreviewBot('Slackbot-LinkExpanding 1.0')).toBe(true);
    expect(isLinkPreviewBot('Googlebot/2.1')).toBe(false); // crawler, not previewer
    expect(isLinkPreviewBot('curl/8.4.0')).toBe(false);
    expect(isLinkPreviewBot(undefined)).toBe(false);
  });
});

describe('hashUserAgent', () => {
  it('produces a stable 16-char hash and "none" for empty', () => {
    expect(hashUserAgent('curl/8.4.0')).toMatch(/^[0-9a-f]{16}$/);
    expect(hashUserAgent('curl/8.4.0')).toBe(hashUserAgent('curl/8.4.0'));
    expect(hashUserAgent('curl/8.4.0')).not.toBe(hashUserAgent('Wget/1.21'));
    expect(hashUserAgent('')).toBe('none');
    expect(hashUserAgent(undefined)).toBe('none');
  });
});

describe('signature lists', () => {
  it('all signatures are lowercase (matcher lowercases input)', () => {
    for (const sig of _internals.ALL_BOT_UAS) {
      expect(sig, `signature must be lowercase: ${sig}`).toBe(sig.toLowerCase());
    }
  });
  it('covers the documented magic-link previewer set', () => {
    for (const sig of ['facebookexternalhit', 'twitterbot', 'slackbot', 'discordbot', 'linkedinbot', 'whatsapp', 'telegrambot', 'applebot']) {
      expect(_internals.LINK_PREVIEW_UAS).toContain(sig);
    }
  });
});

describe('in-app browsers are humans, not link-preview bots (R1-W2 review)', () => {
  it('a real visitor opening the link inside the Snapchat or Pinterest app is not synthetic', () => {
    const snap =
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Snapchat/12.50.0.40 (like Safari/8618.1.15)';
    const pin =
      'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36 [Pinterest/Android]';
    expect(isBotUserAgent(snap)).toBe(false);
    expect(isBotUserAgent(pin)).toBe(false);
  });

  it('the actual preview fetchers are still classified as bots', () => {
    expect(isBotUserAgent('Pinterestbot/1.0 (+http://www.pinterest.com/bot.html)')).toBe(true);
    expect(isBotUserAgent('Snap URL Preview Service; bot')).toBe(true);
  });
});
