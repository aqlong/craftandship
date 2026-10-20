// The email templates and the app's own pages escape interpolated text with
// two copies of one helper (services/email.ts and app.ts). They must agree,
// and both must hold inside an attribute quoted with either quote character:
// a name such as O'Brien in a single-quoted attribute would otherwise end it.

import { describe, it, expect, vi } from 'vitest';

vi.mock('../../src/db/index', () => ({
  default: { query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }), connect: vi.fn(), on: vi.fn() },
}));

import { escapeHtml as escapeForEmail } from '../../src/services/email';
import { escapeHtml as escapeForPage } from '../../src/app';

const INPUTS = [
  '',
  'plain text',
  "O'Brien & Sons",
  '<script>alert(1)</script>',
  `" onmouseover="alert(1)`,
  `' onmouseover='alert(1)`,
  '&amp; already escaped',
  '&#39;&quot;&lt;',
  'a > b < c',
  'https://example.test/x?a=1&b=2',
  `unicode: caf${String.fromCharCode(0xe9)}, ${String.fromCharCode(0x65e5, 0x672c)}`,
];

describe('escapeHtml in services/email.ts', () => {
  it.each(INPUTS)('matches the app helper on %j', (input) => {
    expect(escapeForEmail(input)).toBe(escapeForPage(input));
  });

  it('escapes the apostrophe, so a single-quoted attribute cannot be closed', () => {
    const attribute = `<a title='${escapeForEmail("x' onmouseover='alert(1)")}'>`;
    expect(attribute).toBe("<a title='x&#39; onmouseover=&#39;alert(1)'>");
    expect(escapeForEmail(`"'<>&`)).toBe('&quot;&#39;&lt;&gt;&amp;');
  });
});
