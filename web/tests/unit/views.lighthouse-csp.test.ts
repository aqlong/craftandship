/**
 * Guards for the /lighthouse views under the app's Content-Security-Policy.
 *
 * app.ts sends `script-src 'self' 'nonce-<n>' https://plausible.io` with no
 * 'unsafe-inline', so the browser refuses every inline event-handler
 * attribute (oninput=, onsubmit=, onclick= ...). Two Harbor views used them
 * and were dead in production:
 *
 *  - make-public.ejs: the price slider's oninput never ran, so the amount
 *    shown and the amount submitted (a separate hidden input still holding
 *    the default) could disagree, charging the default price for any slider
 *    position.
 *  - lamp.ejs: the cancel-subscription confirm() never ran, so the form
 *    submitted with no confirmation.
 *
 * These tests fail if an inline handler comes back, and render the two views
 * for real to check the replacement wiring.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';
import ejs from 'ejs';
import { httpUrl } from '../../src/utils/format';

const LIGHTHOUSE_VIEWS = join(__dirname, '..', '..', 'views', 'lighthouse');

/** Remove EJS comment tags so prose that mentions "oninput=" is not scanned. */
function stripEjsComments(source: string): string {
  return source.replace(/<%#[\s\S]*?%>/g, '');
}

/** Replace every remaining EJS tag, so a "<%= x %>" inside an HTML tag does not end that tag early for the scan. */
function stripEjsTags(source: string): string {
  return source.replace(/<%[\s\S]*?%>/g, 'EJS');
}

describe('lighthouse views carry no inline event-handler attributes', () => {
  const files = readdirSync(LIGHTHOUSE_VIEWS).filter((f) => f.endsWith('.ejs'));

  it('finds the lighthouse views', () => {
    expect(files).toEqual(
      expect.arrayContaining(['table.ejs', 'lamp.ejs', 'make-public.ejs', 'preview.ejs'])
    );
  });

  for (const file of files) {
    it(`${file} has no on<event>= attribute (CSP would block it)`, () => {
      const source = stripEjsTags(stripEjsComments(readFileSync(join(LIGHTHOUSE_VIEWS, file), 'utf-8')));
      const hits = source.match(/<[a-z][^<>]*\son[a-z]+\s*=\s*["']/gi) ?? [];
      expect(hits).toEqual([]);
    });
  }

  it('every inline <script> in the lighthouse views carries the CSP nonce', () => {
    for (const file of files) {
      // Replace the nonce expression with a plain token so the tag's own
      // closing `>` is the first `>` the regex meets.
      const source = stripEjsComments(readFileSync(join(LIGHTHOUSE_VIEWS, file), 'utf-8')).replace(
        /<%=\s*cspNonce\s*%>/g,
        'NONCE'
      );
      const inlineScripts = source.match(/<script\b(?![^>]*\bsrc=)[^>]*>/gi) ?? [];
      for (const tag of inlineScripts) {
        expect(tag, `${file}: ${tag}`).toContain('nonce="NONCE"');
      }
    }
  });
});

/** Render make-public.ejs the way the route does, for one kind of payer. */
function renderMakePublic(locals: { emailFixed: boolean; confirmEmail: boolean; prefilledEmail?: string }): string {
  return ejs.render(
    readFileSync(join(LIGHTHOUSE_VIEWS, 'make-public.ejs'), 'utf-8'),
    {
      build: { id: '11111111-2222-3333-4444-555555555555' },
      csrfToken: 'csrf-token-value',
      prefilledEmail: 'maya@example.com',
      amountMinCents: 1900,
      amountMaxCents: 3900,
      amountDefaultCents: 3900,
      cspNonce: 'test-nonce',
      ...locals,
    },
    { filename: join(LIGHTHOUSE_VIEWS, 'make-public.ejs') }
  );
}

describe('make-public.ejs: the address the payment is tied to', () => {
  it('a trial whose build already has an account: the address is shown, not editable, submitted as it is, and must be confirmed', () => {
    const html = renderMakePublic({ emailFixed: true, confirmEmail: true });
    expect(html).not.toMatch(/<input\b[^>]*type="email"/);
    expect(html).toContain('<input type="hidden" name="email" value="maya@example.com">');
    expect(html).toMatch(/<p class="account-email"[^>]*>maya@example\.com<\/p>/);
    const box = html.match(/<input\b[^>]*name="email_confirmed"[^>]*>/);
    expect(box).not.toBeNull();
    expect(box![0]).toContain('type="checkbox"');
    expect(box![0]).toContain('required');
    expect(html).toMatch(/<label for="email-confirmed">This is my email address\./);
    expect(html).toContain('Whoever reads that inbox can sign in to your website');
    expect(html).toContain('write to support@craftandship.io before you pay');
  });

  it('a trial whose build has no account yet: the address is asked for, and must be confirmed', () => {
    const html = renderMakePublic({ emailFixed: false, confirmEmail: true });
    const field = html.match(/<input\b[^>]*type="email"[^>]*>/);
    expect(field).not.toBeNull();
    expect(field![0]).toContain('name="email"');
    expect(field![0]).toContain('value="maya@example.com"');
    expect(html).toMatch(/<input\b[^>]*name="email_confirmed"[^>]*required/);
  });

  it('an account holder: their own address, shown, with nothing to confirm', () => {
    const html = renderMakePublic({ emailFixed: true, confirmEmail: false });
    expect(html).toContain('<input type="hidden" name="email" value="maya@example.com">');
    expect(html).not.toContain('name="email_confirmed"');
  });

  it('escapes the address wherever it is printed', () => {
    const html = renderMakePublic({ emailFixed: true, confirmEmail: true, prefilledEmail: '"><script>x</script>@example.com' });
    expect(html).not.toContain('<script>x</script>');
  });

  // The order on the page is the order the Tab key follows (nothing in the
  // form sets a tabindex): the address, its confirmation, the amount, pay.
  const position = (html: string, pattern: RegExp): number => {
    const at = html.search(pattern);
    expect(at, String(pattern)).toBeGreaterThan(-1);
    return at;
  };
  it.each([
    ['shown as text', true, /<p class="account-email"/],
    ['asked for', false, /<input\b[^>]*type="email"/],
  ] as Array<[string, boolean, RegExp]>)('an address %s comes first, then its confirmation, then the amount, then the button', (_label, emailFixed, address) => {
    const html = renderMakePublic({ emailFixed, confirmEmail: true });
    const order = [
      position(html, address),
      position(html, /<input\b[^>]*name="email_confirmed"/),
      position(html, /<input\b[^>]*id="amount-slider"/),
      position(html, /<input\b[^>]*name="sliding_scale_attestation"/),
      position(html, /<button type="submit"/),
    ];
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(html).not.toMatch(/\btabindex=/);
  });

  it('the confirmation checkbox has a label of its own, and reads out the address it confirms when that address is plain text', () => {
    const fixed = renderMakePublic({ emailFixed: true, confirmEmail: true });
    const box = fixed.match(/<input\b[^>]*name="email_confirmed"[^>]*>/)![0];
    expect(box).toContain('id="email-confirmed"');
    expect(fixed.match(/<label for="email-confirmed">/g)).toHaveLength(1);
    expect(box).toContain('aria-describedby="account-email"');
    // The element it points at exists once and holds the address.
    expect(fixed.match(/\bid="account-email"/g)).toHaveLength(1);
    expect(fixed).toMatch(/<p class="account-email" id="account-email">maya@example\.com<\/p>/);
    // A paragraph is not given an accessible name (ARIA does not allow one there).
    expect(fixed).not.toMatch(/<p\b[^>]*aria-labelledby/);

    // An address field is a Tab stop with its own label, so nothing points at it.
    const asked = renderMakePublic({ emailFixed: false, confirmEmail: true });
    expect(asked).toMatch(/<label for="email">Email for your website's account<\/label>/);
    expect(asked.match(/<input\b[^>]*name="email_confirmed"[^>]*>/)![0]).not.toContain('aria-describedby');
    expect(asked).not.toContain('id="account-email"');
  });
});

describe('make-public.ejs: the price slider is the submitted field', () => {
  const html = renderMakePublic({ emailFixed: false, confirmEmail: true });

  it('submits exactly one monthly_amount_cents field, and it is the range input', () => {
    const named = html.match(/<input\b[^>]*name="monthly_amount_cents"[^>]*>/g) ?? [];
    expect(named).toHaveLength(1);
    expect(named[0]).toContain('type="range"');
    expect(named[0]).toContain('id="amount-slider"');
    expect(named[0]).toContain('step="100"');
  });

  it('has no separate hidden amount input that could drift from the slider', () => {
    expect(html).not.toContain('id="amount-input"');
  });

  it('updates the display and the attestation block from a nonced input listener', () => {
    expect(html).toContain('<script nonce="test-nonce">');
    expect(html).toContain("slider.addEventListener('input'");
    expect(html).toContain("document.getElementById('amount-display').textContent");
    expect(html).toContain('toggleAttestation();');
  });
});

describe('lamp.ejs: cancel-subscription is confirmed by a nonced listener', () => {
  const html = ejs.render(
    readFileSync(join(LIGHTHOUSE_VIEWS, 'lamp.ejs'), 'utf-8'),
    {
      build: {
        id: '11111111-2222-3333-4444-555555555555',
        app_name: 'Sample Studio',
        status: 'live',
        review_status: 'approved',
        review_notes: null,
        preview_url: null,
        live_url: 'https://sample-studio.example.com',
      },
      ownerKind: 'keeper',
      canMakePublic: false,
      showRevisionsNote: false,
      httpUrl,
      subscriptionState: {
        status: 'active',
        cancelAtPeriodEnd: false,
        currentPeriodEnd: null,
        slidingScaleAmountCents: 2900,
      },
      csrfToken: 'csrf-token-value',
      cspNonce: 'test-nonce',
    },
    { filename: join(LIGHTHOUSE_VIEWS, 'lamp.ejs') }
  );

  it('renders the cancel form without an onsubmit attribute', () => {
    const form = html.match(/<form id="cancel-subscription-form"[^>]*>/);
    expect(form).not.toBeNull();
    expect(form![0]).not.toMatch(/\sonsubmit=/i);
    expect(form![0]).toContain('data-confirm="Cancel the subscription?');
  });

  it('attaches a confirm() guard from a nonced script that can cancel the submit', () => {
    expect(html).toContain('<script nonce="test-nonce">');
    expect(html).toContain("getElementById('cancel-subscription-form')");
    expect(html).toMatch(/window\.confirm\(form\.getAttribute\('data-confirm'\)\)/);
    expect(html).toContain('ev.preventDefault()');
  });
});
