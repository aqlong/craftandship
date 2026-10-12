import { describe, it, expect } from 'vitest';
import { displayName, httpUrl } from '../../src/utils/format';

// displayName is used on the public /gallery + feed surfaces (app.ts, gallery.ts)
// to turn a build slug into a human-readable title, so its edge cases are
// customer-visible.
describe('displayName', () => {
  it('title-cases a hyphenated slug', () => {
    expect(displayName('neighborhood-bakery')).toBe('Neighborhood Bakery');
  });

  it('uppercases known acronyms', () => {
    expect(displayName('neighborhood-cms')).toBe('Neighborhood CMS');
    expect(displayName('my-crm-tool')).toBe('My CRM Tool');
    expect(displayName('ai-coach')).toBe('AI Coach');
  });

  it('returns empty string for an empty slug', () => {
    expect(displayName('')).toBe('');
  });

  it('drops empty segments from double / leading / trailing hyphens', () => {
    expect(displayName('--maya--reiki--')).toBe('Maya Reiki');
  });

  it('leaves a single non-acronym word title-cased', () => {
    expect(displayName('lighthouse')).toBe('Lighthouse');
  });

  it('does not mangle already-capitalized input', () => {
    expect(displayName('Fern-Stone')).toBe('Fern Stone');
  });
});


// httpUrl guards every href in the views whose value comes from the database
// (builds.live_url, builds.preview_url), including the public /gallery page.
describe('httpUrl', () => {
  it('passes plain http and https URLs through, trimmed', () => {
    expect(httpUrl('https://studio.example/')).toBe('https://studio.example/');
    expect(httpUrl('  http://localhost:3000/a?b=c#d ')).toBe('http://localhost:3000/a?b=c#d');
    expect(httpUrl('HTTPS://Studio.Example/x')).toBe('HTTPS://Studio.Example/x');
  });

  it('turns script-capable and unknown schemes into "#"', () => {
    for (const hostile of [
      'javascript:alert(1)',
      ' JavaScript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'vbscript:msgbox(1)',
      'file:///etc/passwd',
      '//evil.example/x',
      '/relative/path',
      'studio.example',
    ]) {
      expect(httpUrl(hostile)).toBe('#');
    }
  });

  it('refuses values that could leave the attribute or smuggle whitespace', () => {
    expect(httpUrl('https://x.example/" onmouseover="alert(1)')).toBe('#');
    expect(httpUrl("https://x.example/'><script>")).toBe('#');
    expect(httpUrl('https://x.example/a b')).toBe('#');
    expect(httpUrl('https://x.example/\njavascript:alert(1)')).toBe('#');
  });

  it('returns "#" for anything that is not a string', () => {
    for (const value of [null, undefined, 42, {}, ['https://x.example/']]) {
      expect(httpUrl(value)).toBe('#');
    }
  });
});
