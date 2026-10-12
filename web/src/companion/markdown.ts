/**
 * Tiny markdown -> safe-HTML renderer for companion turns.
 *
 * Haven's prompt encourages bold (**word**), bulleted lists (- item),
 * and paragraph breaks (\n\n). Without rendering those they show up as
 * raw "**" and "-" in the chat UI, which reads as broken software, not
 * letters on warm paper.
 *
 * Scope is intentionally tiny:
 *   - HTML-escape everything first (XSS-safe)
 *   - Group consecutive `- item` lines into <ul><li>...</li></ul>
 *   - Convert **bold** to <strong>
 *   - Convert *italic* to <em>
 *   - Wrap remaining paragraphs in <p>
 *
 * No nested lists. No links. No code. No tables. If we ever need
 * those we should add a real library; for the keeper's table this is
 * enough.
 *
 * The same renderer runs server-side (via app.locals.companionMarkdown
 * for initial page render) AND client-side (via a JS port in the EJS
 * view's script tag for new turns appended via fetch). The two
 * implementations must stay equivalent; the test suite asserts both.
 */

const ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);
}

/**
 * Render a companion turn's plain-text-with-tiny-markdown into safe HTML.
 * Returns a string of `<p>` and `<ul>` blocks.
 */
export function renderCompanionMarkdown(text: string): string {
  const escaped = escapeHtml(text);

  // Split into paragraph blocks on blank lines (\n\n+).
  const blocks = escaped.split(/\n{2,}/);
  const rendered: string[] = [];

  for (const block of blocks) {
    const trimmed = block.trim();
    if (!trimmed) continue;

    // Bullet list: every line starts with "- ".
    const lines = trimmed.split(/\n/);
    const allBullets =
      lines.length > 0 && lines.every((line) => /^\s*-\s+\S/.test(line));

    if (allBullets) {
      const items = lines.map((line) => {
        const itemText = line.replace(/^\s*-\s+/, '');
        return `<li>${applyInline(itemText)}</li>`;
      });
      rendered.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    // Paragraph: keep single newlines as <br> within the block.
    const paragraphHtml = lines.map((l) => applyInline(l)).join('<br>');
    rendered.push(`<p>${paragraphHtml}</p>`);
  }

  return rendered.join('');
}

/**
 * Apply inline transformations (bold, italic) to an already-HTML-escaped
 * fragment. Operates on the escaped form so we don't have to worry
 * about user-supplied HTML.
 */
function applyInline(text: string): string {
  // **bold** before *italic* so a triple-star pattern (***) is
  // tokenized as bold-then-italic-leftover, not the reverse.
  let out = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  return out;
}

/**
 * The same logic as a self-contained JS function string, suitable for
 * inlining into the EJS view's <script> tag so client-side appended
 * turns render with identical output. Kept in this module so server +
 * client never drift.
 */
export const RENDER_COMPANION_MARKDOWN_CLIENT_JS = `
function renderCompanionMarkdown(text) {
  var escapeMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  var escaped = String(text).replace(/[&<>"']/g, function(ch) { return escapeMap[ch]; });
  var blocks = escaped.split(/\\n{2,}/);
  var out = [];
  for (var i = 0; i < blocks.length; i++) {
    var trimmed = blocks[i].trim();
    if (!trimmed) continue;
    var lines = trimmed.split(/\\n/);
    var allBullets = lines.length > 0 && lines.every(function(line) { return /^\\s*-\\s+\\S/.test(line); });
    if (allBullets) {
      var items = lines.map(function(line) {
        var item = line.replace(/^\\s*-\\s+/, '');
        return '<li>' + applyInline(item) + '</li>';
      });
      out.push('<ul>' + items.join('') + '</ul>');
    } else {
      var paragraphs = lines.map(function(l) { return applyInline(l); }).join('<br>');
      out.push('<p>' + paragraphs + '</p>');
    }
  }
  function applyInline(text) {
    var o = text.replace(/\\*\\*([^*\\n]+)\\*\\*/g, '<strong>$1</strong>');
    o = o.replace(/(^|[^*])\\*([^*\\n]+)\\*/g, '$1<em>$2</em>');
    return o;
  }
  return out.join('');
}
`.trim();
