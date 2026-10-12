import { describe, it, expect } from 'vitest';
import {
  renderCompanionMarkdown,
  RENDER_COMPANION_MARKDOWN_CLIENT_JS,
} from '../../../src/companion/markdown';

describe('renderCompanionMarkdown', () => {
  it('wraps a single paragraph in <p>', () => {
    expect(renderCompanionMarkdown('Hello there.')).toBe('<p>Hello there.</p>');
  });

  it('splits paragraphs on blank lines', () => {
    expect(renderCompanionMarkdown('First.\n\nSecond.')).toBe(
      '<p>First.</p><p>Second.</p>'
    );
  });

  it('preserves single newlines as <br> within a paragraph', () => {
    expect(renderCompanionMarkdown('line one\nline two')).toBe(
      '<p>line one<br>line two</p>'
    );
  });

  it('renders **bold** as <strong>', () => {
    expect(renderCompanionMarkdown('hello **world** there')).toBe(
      '<p>hello <strong>world</strong> there</p>'
    );
  });

  it('renders *italic* as <em>', () => {
    expect(renderCompanionMarkdown('hello *world* there')).toBe(
      '<p>hello <em>world</em> there</p>'
    );
  });

  it('renders bullet lists as <ul><li>', () => {
    const md = '- one\n- two\n- three';
    expect(renderCompanionMarkdown(md)).toBe(
      '<ul><li>one</li><li>two</li><li>three</li></ul>'
    );
  });

  it('renders bold inside list items', () => {
    const md = '- **Home:** the entry\n- **About:** the story';
    expect(renderCompanionMarkdown(md)).toBe(
      '<ul><li><strong>Home:</strong> the entry</li>' +
        '<li><strong>About:</strong> the story</li></ul>'
    );
  });

  it('handles a mix of paragraphs, bold, and lists in one turn', () => {
    const md =
      'Got it. Here is what I am hearing:\n\n' +
      '- An easy way to **chat**\n' +
      '- Upcoming **events** visible\n' +
      '- A newsletter\n\n' +
      'Does that feel right?';
    const out = renderCompanionMarkdown(md);
    expect(out).toContain('<p>Got it. Here is what I am hearing:</p>');
    expect(out).toContain('<ul>');
    expect(out).toContain('<strong>chat</strong>');
    expect(out).toContain('<strong>events</strong>');
    expect(out).toContain('<p>Does that feel right?</p>');
  });

  it('html-escapes raw markup in keeper-supplied or model-supplied text', () => {
    const md = 'do not <script>alert(1)</script> please';
    const out = renderCompanionMarkdown(md);
    expect(out).not.toContain('<script>');
    expect(out).toContain('&lt;script&gt;');
  });

  it('html-escapes ampersands and quotes', () => {
    expect(renderCompanionMarkdown('Tom & Jerry "go"')).toBe(
      '<p>Tom &amp; Jerry &quot;go&quot;</p>'
    );
  });

  it('handles an empty string gracefully', () => {
    expect(renderCompanionMarkdown('')).toBe('');
  });

  it('does not match a single trailing asterisk (e.g. "what *now")', () => {
    // Italic requires a paired closing *. A lone * stays literal.
    expect(renderCompanionMarkdown('what *now')).toBe('<p>what *now</p>');
  });

  it('handles asterisks across newlines (does not span a paragraph break)', () => {
    // Italic regex disallows newline within the italic content, so
    // mismatched * across paragraphs stays literal.
    expect(renderCompanionMarkdown('open *here\n\nclose* there')).toBe(
      '<p>open *here</p><p>close* there</p>'
    );
  });

  it('client-side JS renderer mirrors the server output', () => {
    // Build a sandboxed JS environment, paste the client JS, and run it
    // against the same fixtures the server-side renderer hit. Both must
    // produce identical HTML.
    const fn = new Function(
      RENDER_COMPANION_MARKDOWN_CLIENT_JS + '\nreturn renderCompanionMarkdown;'
    );
    const clientRenderer = fn() as (text: string) => string;

    const fixtures = [
      'Hello there.',
      'First.\n\nSecond.',
      'line one\nline two',
      'hello **world** there',
      'hello *world* there',
      '- one\n- two\n- three',
      '- **Home:** entry\n- **About:** story',
      'do not <script>alert(1)</script> please',
      'Tom & Jerry "go"',
      '',
      '5*5*5',
      'Got it.\n\n- A\n- B\n\nMore.',
    ];

    for (const md of fixtures) {
      expect(clientRenderer(md), `parity for: ${JSON.stringify(md)}`).toBe(
        renderCompanionMarkdown(md)
      );
    }
  });
});
