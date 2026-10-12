// tests/unit/email.service.test.ts
// Unit tests for src/services/email.ts

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

// Mock the database pool before importing the service
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
    },
  };
});

import pool from '../../src/db/index';
import {
  generateUnsubscribeToken,
  sendWelcomeEmail,
  sendBuildQueuedEmail,
  sendPasswordResetEmail,
  sendPreviewReadyEmail,
  sendAppLiveEmail,
  sendReviewReadyEmail,
  sendReviewChangesRequestedEmail,
  sendHarborMadePublicEmail,
  sendHarborLiveEmail,
  sendPaymentNotAppliedEmail,
} from '../../src/services/email';

import { getSessionSecret, SESSION_SECRET_PLACEHOLDERS } from '../../src/middleware/auth';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;

// --- generateUnsubscribeToken ---

describe('generateUnsubscribeToken', () => {
  it('returns a hex string', () => {
    const token = generateUnsubscribeToken('user@example.com');
    expect(token).toMatch(/^[0-9a-f]{64}$/);
  });

  it('returns the same token for the same email (deterministic with SESSION_SECRET)', () => {
    const token1 = generateUnsubscribeToken('user@example.com');
    const token2 = generateUnsubscribeToken('user@example.com');
    expect(token1).toBe(token2);
  });

  it('returns different tokens for different emails', () => {
    const token1 = generateUnsubscribeToken('alice@example.com');
    const token2 = generateUnsubscribeToken('bob@example.com');
    expect(token1).not.toBe(token2);
  });

  it('is signed with the resolved session secret, never with a placeholder literal', () => {
    const email = 'user@example.com';
    const hmac = (secret: string): string =>
      crypto.createHmac('sha256', secret).update(email).digest('hex');
    const previous = process.env.SESSION_SECRET;
    try {
      // Even with the variable gone, the token must not fall back to a value
      // that ships in the repository (anyone could forge it).
      delete process.env.SESSION_SECRET;
      const token = generateUnsubscribeToken(email);
      expect(token).toBe(hmac(getSessionSecret()));
      for (const placeholder of SESSION_SECRET_PLACEHOLDERS) {
        expect(token).not.toBe(hmac(placeholder));
      }
    } finally {
      if (previous === undefined) delete process.env.SESSION_SECRET;
      else process.env.SESSION_SECRET = previous;
    }
  });

  it('no file under src/ falls back to a literal for a secret-bearing variable', () => {
    // Guards the whole class of bug, not just this one call site: a
    // `process.env.X_SECRET || '...'` (or KEY / TOKEN / PASSWORD) fallback
    // turns a missing variable into a publicly known credential.
    const srcRoot = path.join(__dirname, '..', '..', 'src');
    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (entry.name.endsWith('.ts')) {
          const lines = fs.readFileSync(full, 'utf-8').split('\n');
          lines.forEach((line, index) => {
            if (/process\.env\.[A-Z0-9_]*(SECRET|TOKEN|PASSWORD|_KEY)\b\s*(\|\||\?\?)\s*['"`][^'"`]/.test(line)) {
              offenders.push(`${path.relative(srcRoot, full)}:${index + 1}`);
            }
          });
        }
      }
    };
    walk(srcRoot);
    expect(offenders).toEqual([]);
  });
});

// --- emailShell (tested indirectly via exported send* functions) ---
// We verify the HTML output by inspecting the html_body stored via logEmail,
// which is captured in the mockQuery calls.

describe('emailShell unsubscribe link', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    // isUnsubscribed query returns not subscribed
    mockQuery.mockResolvedValue({ rows: [] });
    delete process.env.RESEND_API_KEY;
  });

  it('includes an unsubscribe link in the footer when unsubscribeUrl is provided', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        // logEmail call: html_body is the 10th param (index 9)
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendWelcomeEmail('user@example.com', 'Alice');
    expect(capturedHtml).toContain('Unsubscribe');
    expect(capturedHtml).toContain('/unsubscribe?email=');
  });

  it('omits unsubscribe link when no URL is provided (password reset email)', async () => {
    let capturedHtml = '';
    // Password reset is transactional: there is no isUnsubscribed lookup, so
    // the first query is the email_log insert.
    mockQuery.mockImplementationOnce((_sql: string, params: unknown[]) => {
      capturedHtml = (params as string[])[9] ?? '';
      return Promise.resolve({ rows: [] });
    });

    await sendPasswordResetEmail('user@example.com', 'Alice', 'https://craftandship.io/reset-password?token=abc123');
    expect(capturedHtml.length).toBeGreaterThan(100);
    expect(capturedHtml).toContain('reset-password');
    expect(capturedHtml).not.toContain('Unsubscribe');
  });

  it('always includes reply-to in the Resend payload when API key is set', async () => {
    process.env.RESEND_API_KEY = 'test-key-abc';
    process.env.RESEND_FROM_EMAIL = 'Craft & Ship <noreply@craftandship.io>';

    let capturedBody = '';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'resend-id-123' }),
    });
    vi.stubGlobal('fetch', mockFetch);

    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockResolvedValueOnce({ rows: [] }); // logEmail

    await sendWelcomeEmail('user@example.com', 'Alice');

    expect(mockFetch).toHaveBeenCalledOnce();
    capturedBody = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect((capturedBody as Record<string, unknown>).reply_to).toEqual(['support@craftandship.io']);

    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
    delete process.env.RESEND_FROM_EMAIL;
  });
});

// --- sendEmail behavior ---

describe('sendEmail', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    delete process.env.RESEND_API_KEY;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('skips sending and logs "skipped" with "unsubscribed" when recipient is unsubscribed', async () => {
    // isUnsubscribed returns true
    mockQuery.mockResolvedValueOnce({ rows: [{ email_unsubscribed: true }] });
    // logEmail
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    await sendWelcomeEmail('unsub@example.com', 'Bob');

    expect(mockFetch).not.toHaveBeenCalled();
    // logEmail insert was called
    expect(mockQuery).toHaveBeenCalledTimes(2);
    const logCall = mockQuery.mock.calls[1];
    // status param is index 3 (4th param)
    expect(logCall[1][3]).toBe('skipped');
    // errorMessage param is index 5
    expect(logCall[1][5]).toBe('unsubscribed');
  });

  it('logs "skipped" with "RESEND_API_KEY not set" when no API key is configured', async () => {
    // isUnsubscribed returns false
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // logEmail
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    await sendWelcomeEmail('user@example.com', 'Carol');

    expect(mockFetch).not.toHaveBeenCalled();
    const logCall = mockQuery.mock.calls[1];
    expect(logCall[1][3]).toBe('skipped');
    expect(logCall[1][5]).toBe('RESEND_API_KEY not set');
  });

  it('calls Resend fetch with correct payload fields when API key is set and recipient is not unsubscribed', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';
    process.env.RESEND_FROM_EMAIL = 'Craft & Ship <noreply@craftandship.io>';

    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'resend-abc' }),
    });
    vi.stubGlobal('fetch', mockFetch);

    // isUnsubscribed, then logEmail
    mockQuery.mockResolvedValue({ rows: [] });

    await sendBuildQueuedEmail('dev@example.com', 'Dave', 'MyApp');

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://api.resend.com/emails');
    expect(init.method).toBe('POST');

    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.to).toEqual(['dev@example.com']);
    expect(body.subject).toContain('MyApp');
    expect(body.from).toBe('Craft & Ship <noreply@craftandship.io>');
    expect(body.reply_to).toEqual(['support@craftandship.io']);
    expect(typeof body.html).toBe('string');
  });

  it('logs "sent" status with resend_id on Resend 200 response', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';

    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'resend-id-sent-test' }),
    });
    vi.stubGlobal('fetch', mockFetch);

    mockQuery.mockResolvedValue({ rows: [] });

    await sendWelcomeEmail('sent@example.com', 'Eve');

    const logCall = mockQuery.mock.calls[1];
    expect(logCall[1][3]).toBe('sent');
    // resendId is index 6
    expect(logCall[1][6]).toBe('resend-id-sent-test');
  });

  it('logs "failed" status with status code and error body on Resend 4xx response', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';

    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      text: async () => '{"message":"Invalid email address"}',
    });
    vi.stubGlobal('fetch', mockFetch);

    mockQuery.mockResolvedValue({ rows: [] });

    await sendWelcomeEmail('bad@example.com', 'Frank');

    const logCall = mockQuery.mock.calls[1];
    expect(logCall[1][3]).toBe('failed');
    // statusCode is index 4
    expect(logCall[1][4]).toBe(422);
    // errorMessage is index 5
    expect(logCall[1][5]).toContain('Invalid email address');
  });
});

// --- sendPreviewReadyEmail: magic link support ---

describe('sendPreviewReadyEmail', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    delete process.env.RESEND_API_KEY;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('accepts magicLinkUrl parameter and includes magic link button in HTML', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendPreviewReadyEmail(
      'user@example.com', 'Alice', 'TestApp',
      'https://testapp.up.railway.app', 'build-uuid-123',
      'admin@example.test', 'https://testapp.up.railway.app/magic?token=abc123'
    );

    expect(capturedHtml).toContain('Log In to Your Website');
    // The stored copy keeps the link but never the single-use secret.
    expect(capturedHtml).toContain('/magic?token=[redacted]');
    expect(capturedHtml).not.toContain('abc123');
    expect(capturedHtml).toContain('expires in 24 hours');
    expect(capturedHtml).toContain('admin@example.test');
  });

  it('shows fallback messaging when magicLinkUrl is not provided', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendPreviewReadyEmail(
      'user@example.com', 'Alice', 'TestApp',
      'https://testapp.up.railway.app', 'build-uuid-123'
    );

    expect(capturedHtml).toContain('Login instructions have been sent separately');
    expect(capturedHtml).not.toContain('Log In to Your Website');
  });

  it('does NOT contain password123 in the email HTML', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendPreviewReadyEmail(
      'user@example.com', 'Alice', 'TestApp',
      'https://testapp.up.railway.app', 'build-uuid-123',
      'admin@example.test', 'https://testapp.up.railway.app/magic?token=abc'
    );

    expect(capturedHtml.length).toBeGreaterThan(200);
    expect(capturedHtml).not.toContain('password123');
  });
});

// --- sendAppLiveEmail: no password123 ---

describe('sendAppLiveEmail', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    delete process.env.RESEND_API_KEY;
  });

  it('does NOT contain password123 in the email HTML', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendAppLiveEmail('user@example.com', 'Alice', 'TestApp', 'https://testapp.up.railway.app', 'build-uuid-123');

    expect(capturedHtml.length).toBeGreaterThan(200);
    expect(capturedHtml).not.toContain('password123');
  });
});

// --- review emails (senior engineer review gate) ---

// What the two emails around the review tell the customer to do, and when.
// The preview-ready email goes out before any verdict; the status page offers
// "Approve and go live" only once the review is approved (views/status.ejs),
// so the email must not send the customer to click it. The review-ready email
// goes out when that verdict is recorded.
describe('the emails around the review say what the status page offers at that moment', () => {
  const plainText = (html: string): string => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
  async function htmlOf(send: () => Promise<void>): Promise<string> {
    let capturedHtml = '';
    mockQuery.mockReset();
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });
    delete process.env.RESEND_API_KEY;
    await send();
    return capturedHtml;
  }

  it('preview ready: the review comes first, another email follows, and only then does the page offer the button', async () => {
    const text = plainText(
      await htmlOf(() => sendPreviewReadyEmail('user@example.com', 'Alice', 'TestApp', 'https://testapp.example.test', 'build-uuid-123'))
    );
    expect(text).toContain(
      'Your website needs a recorded review approval before it can go live. When the review is done you will get another email, and your build status page will then offer Approve and Go Live .'
    );
    expect(text).not.toMatch(/When you are satisfied|make it official|click "Approve and Go Live"/);
    // In the order the worker's own preview-ready email uses: the preview
    // address, the login, the review sentence, then how to ask for changes.
    const order = ['https://testapp.example.test', 'Login', 'Your website needs a recorded review approval before it can go live', 'If anything needs changing'].map((marker) =>
      text.indexOf(marker)
    );
    expect(order.every((at) => at >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it('preview ready says, word for word, what the worker\'s preview-ready email says about the review', () => {
    // queue/mailer.py sends the same notice when the worker itself brings a
    // build to preview; the two must not tell the customer different things.
    const mailer = fs
      .readFileSync(path.join(__dirname, '../../../queue/mailer.py'), 'utf-8')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ');
    const sentence =
      'Your website needs a recorded review approval before it can go live. When the review is done you will get another email, and your build status page will then offer Approve and Go Live .';
    expect(mailer).toContain(sentence);
    const web = fs.readFileSync(path.join(__dirname, '../../src/services/email.ts'), 'utf-8').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
    expect(web).toContain(sentence);
  });

  it('review ready: click the button; if the subscription has not started that is where it leads; the address does not change', async () => {
    const text = plainText(await htmlOf(() => sendReviewReadyEmail('customer@example.com', 'My App', 'abc-123-build')));
    expect(text).toContain('When you are ready, open your status page and click Approve and Go Live .');
    expect(text).toContain("If your website's subscription has not started yet, that button takes you to start it, and your website goes live when it does.");
    expect(text).toContain('Its address stays the one you have been previewing.');
    expect(text).not.toMatch(/promote the preview to your live URL/);
  });
});

describe('sendReviewReadyEmail', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    delete process.env.RESEND_API_KEY;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('calls sendEmail with correct recipient and subject', async () => {
    process.env.RESEND_API_KEY = 'test-key';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'resend-review-ready' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendReviewReadyEmail('customer@example.com', 'My App', 'build-uuid-xyz');

    expect(mockFetch).toHaveBeenCalledOnce();
    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.to).toEqual(['customer@example.com']);
    expect(body.subject).toMatch(/ready to go live/i);
  });

  it('body includes link to /builds/:buildId', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendReviewReadyEmail('customer@example.com', 'My App', 'abc-123-build');
    expect(capturedHtml).toContain('/builds/abc-123-build');
  });

  it('skips send when recipient is unsubscribed (fire-and-forget)', async () => {
    // isUnsubscribed returns true
    mockQuery.mockResolvedValueOnce({ rows: [{ email_unsubscribed: true }] });
    // logEmail (skipped row)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    // With a key configured, the only thing that can stop the send is the
    // unsubscribe check (without a key the send is skipped anyway).
    process.env.RESEND_API_KEY = ['re', 'test', 'key'].join('_');

    await expect(
      sendReviewReadyEmail('unsub@example.com', 'My App', 'build-uuid-xyz')
    ).resolves.toBeUndefined();

    expect(mockFetch).not.toHaveBeenCalled();
    expect(mockQuery).toHaveBeenCalledTimes(2);
    expect(mockQuery.mock.calls[1][1][3]).toBe('skipped');
    expect(mockQuery.mock.calls[1][1][5]).toBe('unsubscribed');
  });

  it('logs to email_log with template=review_ready via sendEmail wrapper', async () => {
    mockQuery.mockResolvedValue({ rows: [] });

    await sendReviewReadyEmail('customer@example.com', 'My App', 'build-uuid-xyz');

    // Second query is the logEmail INSERT
    const logCall = mockQuery.mock.calls[1];
    // template is the first INSERT param (index 0)
    expect(logCall[1][0]).toBe('review_ready');
  });
});

describe('sendReviewChangesRequestedEmail', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    delete process.env.RESEND_API_KEY;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('body contains review_notes HTML-escaped (XSS canary)', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendReviewChangesRequestedEmail(
      'customer@example.com',
      'My App',
      'build-uuid-xyz',
      '<script>alert(1)</script>'
    );

    expect(capturedHtml).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(capturedHtml).not.toContain('<script>alert(1)</script>');
  });

  it('subject matches "suggestions for your website"', async () => {
    process.env.RESEND_API_KEY = 'test-key';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'resend-review-changes' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendReviewChangesRequestedEmail(
      'customer@example.com',
      'My App',
      'build-uuid-xyz',
      'Change the hero color please.'
    );

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.subject).toBe('We have some suggestions for your website');
  });

  it('body includes link to /builds/:buildId', async () => {
    let capturedHtml = '';
    mockQuery
      .mockResolvedValueOnce({ rows: [] }) // isUnsubscribed
      .mockImplementationOnce((_sql: string, params: unknown[]) => {
        capturedHtml = (params as string[])[9] ?? '';
        return Promise.resolve({ rows: [] });
      });

    await sendReviewChangesRequestedEmail(
      'customer@example.com',
      'My App',
      'deadbeef-42',
      'Please darken the primary button.'
    );
    expect(capturedHtml).toContain('/builds/deadbeef-42');
  });

  it('respects isUnsubscribed and logs to email_log with template=review_changes_requested', async () => {
    // isUnsubscribed returns true
    mockQuery.mockResolvedValueOnce({ rows: [{ email_unsubscribed: true }] });
    // logEmail (skipped)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    await expect(
      sendReviewChangesRequestedEmail(
        'unsub@example.com',
        'My App',
        'build-uuid-xyz',
        'Notes.'
      )
    ).resolves.toBeUndefined();

    expect(mockFetch).not.toHaveBeenCalled();
    const logCall = mockQuery.mock.calls[1];
    expect(logCall[1][0]).toBe('review_changes_requested');
    expect(logCall[1][3]).toBe('skipped');
  });
});

// --- sendHarborLiveEmail: delivery email (no caller in this repository) ---

describe('sendHarborLiveEmail (delivery email of the external review path)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    delete process.env.RESEND_API_KEY;
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('logs with template "harbor_live" + skipped status when API key unset', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] }); // isUnsubscribed
    mockQuery.mockResolvedValueOnce({ rows: [] }); // logEmail
    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    await sendHarborLiveEmail({
      to: 'maya@example.com',
      name: 'Maya',
      appName: 'Sample Bodywork Studio',
      liveUrl: 'https://sample-bodywork-example.up.railway.app',
      buildId: 'build-uuid-abcdef123456',
    });

    expect(mockFetch).not.toHaveBeenCalled();
    const logCall = mockQuery.mock.calls[1];
    expect(logCall[1][0]).toBe('harbor_live');
    expect(logCall[1][3]).toBe('skipped');
  });

  it('sends a live + free + name-your-price email with the URL and no em dashes', async () => {
    process.env.RESEND_API_KEY = 'live-key';
    let sentBody = '';
    const mockFetch = vi.fn().mockImplementation((_url, opts) => {
      sentBody = JSON.parse(opts.body).html;
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendHarborLiveEmail({
      to: 'maya@example.com',
      name: 'Maya',
      appName: 'Sample Bodywork Studio',
      liveUrl: 'https://sample-bodywork-example.up.railway.app',
      buildId: 'build-uuid-abcdef123456',
    });

    expect(mockFetch).toHaveBeenCalled();
    expect(sentBody).toContain('https://sample-bodywork-example.up.railway.app');
    expect(sentBody).toMatch(/live/i);
    expect(sentBody).toMatch(/name your price/i);
    expect(sentBody).not.toContain('\u2014'); // brand voice: no em dashes
  });
});

describe('sendHarborMadePublicEmail', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    delete process.env.RESEND_API_KEY;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('logs with template "harbor_made_public" and skipped status when API key unset', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] }); // isUnsubscribed
    mockQuery.mockResolvedValueOnce({ rows: [] }); // logEmail
    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    await sendHarborMadePublicEmail({
      to: 'keeper@example.com',
      name: 'Maya',
      appName: 'Sample Studio',
      publicUrl: 'https://sample-studio-example.up.railway.app',
      buildId: 'build-uuid-1234567890',
    });

    expect(mockFetch).not.toHaveBeenCalled();
    const logCall = mockQuery.mock.calls[1];
    expect(logCall[1][0]).toBe('harbor_made_public');
    expect(logCall[1][3]).toBe('skipped');
  });

  it('subject includes appName + short build id', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'resend-1' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendHarborMadePublicEmail({
      to: 'k@example.test',
      name: 'Lena',
      appName: 'Lena Mobility',
      publicUrl: 'https://lena.example.com',
      buildId: 'abcdef1234-rest-of-uuid',
    });

    const body = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string) as Record<string, unknown>;
    expect(body.subject).toContain('Lena Mobility');
    expect(body.subject).toContain('abcdef12');
    expect(body.subject).toContain('public');
  });

  it('omits short id from subject when buildId not provided', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'r' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendHarborMadePublicEmail({
      to: 'k@example.test',
      name: 'Maya',
      appName: 'Wellness',
      publicUrl: 'https://example.test',
    });

    const body = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string) as Record<string, unknown>;
    expect(body.subject).toBe('Your website is public: Wellness');
  });

  it('HTML escapes appName + name + publicUrl', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'r' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendHarborMadePublicEmail({
      to: 'k@example.test',
      name: '<script>alert(1)</script>',
      appName: 'Sample & Studio',
      publicUrl: 'https://example.test/?a=b&c=d',
    });

    const html = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string).html as string;
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('Sample &amp; Studio');
  });

  it('contains an unsubscribe link (customer-facing email, not operational)', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'r' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendHarborMadePublicEmail({
      to: 'keeper@example.com',
      name: 'Maya',
      appName: 'Wellness',
      publicUrl: 'https://r.example.com',
    });

    const html = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string).html as string;
    expect(html).toContain('/unsubscribe');
  });

  it('body has no em dashes (brand voice)', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'r' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendHarborMadePublicEmail({
      to: 'keeper@example.com',
      name: 'Maya',
      appName: 'Wellness',
      publicUrl: 'https://r.example.com',
    });

    const html = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string).html as string;
    expect(html).not.toMatch(/\u2014/);
    expect(html).not.toMatch(/&mdash;/i);
  });

  it('body links to /lighthouse/table for the keeper to find the blessing', async () => {
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 'r' }),
    });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });

    await sendHarborMadePublicEmail({
      to: 'keeper@example.com',
      name: 'Maya',
      appName: 'Wellness',
      publicUrl: 'https://r.example.com',
    });

    const html = JSON.parse((mockFetch.mock.calls[0][1] as RequestInit).body as string).html as string;
    expect(html).toContain('/lighthouse/table');
  });

  it('respects unsubscribe (logs skipped, no fetch)', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ email_unsubscribed: true }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);

    await sendHarborMadePublicEmail({
      to: 'unsub@example.com',
      name: 'X',
      appName: 'Y',
      publicUrl: 'https://z.com',
    });

    expect(mockFetch).not.toHaveBeenCalled();
    const logCall = mockQuery.mock.calls[1];
    expect(logCall[1][3]).toBe('skipped');
    expect(logCall[1][5]).toBe('unsubscribed');
  });
});


// ---------------------------------------------------------------------------
// Review round additions
// ---------------------------------------------------------------------------

describe('email links are absolute', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    delete process.env.RESEND_API_KEY;
    delete process.env.BASE_URL;
  });

  function captureLoggedHtml(): () => string {
    let html = '';
    mockQuery.mockReset();
    mockQuery.mockImplementation((sql: string, params: unknown[]) => {
      if (/INSERT INTO email_log/i.test(sql)) html = (params as string[])[9] ?? '';
      return Promise.resolve({ rows: [] });
    });
    return () => html;
  }

  it('puts an absolute https unsubscribe URL in the body (a relative href is dead in an email client)', async () => {
    const getHtml = captureLoggedHtml();
    await sendWelcomeEmail('user@example.com', 'Alice');
    const html = getHtml();
    const match = html.match(/<a href="([^"]*unsubscribe[^"]*)"/);
    expect(match).not.toBeNull();
    expect(match![1]).toMatch(/^https:\/\/[^/]+\/unsubscribe\?email=user%40example\.com&token=/);
  });

  it('honours BASE_URL for every link in the template', async () => {
    process.env.BASE_URL = 'https://staging.example.test/';
    const getHtml = captureLoggedHtml();
    await sendHarborLiveEmail({
      to: 'u@example.com', name: 'U', appName: 'Site', liveUrl: 'https://site.example.test', buildId: 'abcdef12-0000',
    });
    const html = getHtml();
    expect(html).toContain('https://staging.example.test/lighthouse/table');
    expect(html).toContain('https://staging.example.test/unsubscribe?email=');
    expect(html).toContain('https://staging.example.test/brand-harbor/lighthouse-logo-96.png');
    expect(html).not.toContain('https://craftandship.io');
    delete process.env.BASE_URL;
  });

  it('uses the same absolute URL in the List-Unsubscribe header', async () => {
    process.env.RESEND_API_KEY = 'k';
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    vi.stubGlobal('fetch', mockFetch);
    await sendWelcomeEmail('user@example.com', 'Alice');
    const body = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect(body.headers['List-Unsubscribe']).toMatch(/^<https:\/\/[^/]+\/unsubscribe\?email=/);
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });
});

describe('secrets in email links', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    process.env.RESEND_API_KEY = 'k';
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('sends the real reset link but stores a redacted copy in email_log', async () => {
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    vi.stubGlobal('fetch', mockFetch);
    // Assembled at runtime so no secret-shaped literal sits in the source.
    const secret = ['tok', 'en', 'value', '0123456789abcdef'].join('');
    await sendPasswordResetEmail('user@example.com', 'Alice', `https://craftandship.io/reset-password?token=${secret}`);

    const sent = JSON.parse(mockFetch.mock.calls[0][1].body as string) as { html: string };
    expect(sent.html).toContain(secret);

    const insert = mockQuery.mock.calls.find((c) => /INSERT INTO email_log/i.test(c[0] as string));
    expect(insert).toBeDefined();
    const stored = (insert![1] as string[])[9];
    expect(stored).toContain('reset-password?token=[redacted]');
    expect(stored).not.toContain(secret);
  });

  it('also redacts the unsubscribe token in the stored copy', async () => {
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    vi.stubGlobal('fetch', mockFetch);
    await sendWelcomeEmail('user@example.com', 'Alice');
    const insert = mockQuery.mock.calls.find((c) => /INSERT INTO email_log/i.test(c[0] as string));
    const stored = (insert![1] as string[])[9];
    expect(stored).toContain('/unsubscribe?email=user%40example.com&token=[redacted]');
    expect(stored).not.toContain(generateUnsubscribeToken('user@example.com'));
  });
});

describe('unsubscribe does not suppress transactional mail', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    process.env.RESEND_API_KEY = 'k';
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  it('still sends a password reset to a customer who unsubscribed from marketing', async () => {
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockImplementation((sql: string) =>
      /FROM customers/i.test(sql) ? Promise.resolve({ rows: [{ email_unsubscribed: true }] }) : Promise.resolve({ rows: [] })
    );
    await sendPasswordResetEmail('gone@example.com', 'G', 'https://craftandship.io/reset-password?token=zz');
    expect(mockFetch).toHaveBeenCalledOnce();
    const body = JSON.parse(mockFetch.mock.calls[0][1].body as string);
    expect(body.headers).toBeUndefined();
  });

  it('still skips a lifecycle email for the same unsubscribed customer', async () => {
    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockImplementation((sql: string) =>
      /FROM customers/i.test(sql) ? Promise.resolve({ rows: [{ email_unsubscribed: true }] }) : Promise.resolve({ rows: [] })
    );
    await sendWelcomeEmail('gone@example.com', 'G');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('passes an abort signal so a hung Resend call cannot hold the socket open forever', async () => {
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    vi.stubGlobal('fetch', mockFetch);
    mockQuery.mockResolvedValue({ rows: [] });
    await sendWelcomeEmail('a@example.com', 'A');
    expect((mockFetch.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
  });
});

describe('nurture emails match the single-subscription product', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    delete process.env.RESEND_API_KEY;
  });

  async function loggedHtmlOf(send: () => Promise<void>): Promise<string> {
    let html = '';
    mockQuery.mockReset();
    mockQuery.mockImplementation((sql: string, params: unknown[]) => {
      if (/INSERT INTO email_log/i.test(sql)) html = (params as string[])[9] ?? '';
      return Promise.resolve({ rows: [] });
    });
    await send();
    return html;
  }

  it('how-it-works no longer quotes the retired $299 build fee or a 4-revision cap', async () => {
    const { sendWelcomeHowItWorksEmail } = await import('../../src/services/email');
    const html = await loggedHtmlOf(() => sendWelcomeHowItWorksEmail('a@example.com', 'A'));
    expect(html.length).toBeGreaterThan(200);
    expect(html).not.toMatch(/\$299|\$499|4 free revisions/);
    expect(html).not.toContain('business day');
    expect(html).toContain('unlimited');
  });

  it('how-it-works does not call the preview private', async () => {
    const { sendWelcomeHowItWorksEmail } = await import('../../src/services/email');
    const html = await loggedHtmlOf(() => sendWelcomeHowItWorksEmail('a@example.com', 'A'));
    expect(html).toContain('built, free, in a preview');
    expect(html).not.toMatch(/private/i);
  });

  it('day-7 email no longer promises a limited-time beta price', async () => {
    const { sendWelcomeBetaUrgencyEmail } = await import('../../src/services/email');
    const html = await loggedHtmlOf(() => sendWelcomeBetaUrgencyEmail('a@example.com', 'A'));
    expect(html.length).toBeGreaterThan(200);
    expect(html).not.toMatch(/\$299|\$499|[Bb]eta|pricing goes away/);
  });

  it('day-7 email promises a review before going live, not a named person reviewing every page', async () => {
    const { sendWelcomeBetaUrgencyEmail } = await import('../../src/services/email');
    const html = await loggedHtmlOf(() => sendWelcomeBetaUrgencyEmail('a@example.com', 'A'));
    expect(html).toContain('every website needs a recorded review approval before it goes live');
    expect(html).not.toMatch(/personally reviews/);
  });
});

describe('customer emails say who builds and who reviews as the code does it', () => {
  // The site is assembled by software from a library of tested building blocks.
  // The worker emails the customer the preview address when the build arrives at
  // preview, and the review comes after that: so no email may say that a
  // Senior Web Craftsman built the site or personally reviewed it before the
  // customer got the address. (The two review-gate emails are sent when an admin
  // records a verdict, which is a person's review, and say so.)
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    delete process.env.RESEND_API_KEY;
  });

  async function htmlOf(send: () => Promise<void>): Promise<string> {
    let html = '';
    mockQuery.mockReset();
    mockQuery.mockImplementation((sql: string, params: unknown[]) => {
      if (/INSERT INTO email_log/i.test(sql)) html = (params as string[])[9] ?? '';
      return Promise.resolve({ rows: [] });
    });
    await send();
    return html;
  }

  const CLAIMS: Array<[RegExp, string]> = [
    [/personally (?:reviews?|reviewed|builds?)/i, 'personally reviews'],
    [/Senior Web Craftsman (?:builds|reviews|reviewed)|reviewed by a Senior Web Craftsman/i, 'a Senior Web Craftsman builds or reviews'],
    [/co-?created with you and a Senior Web Craftsman/i, 'co-created with a Senior Web Craftsman'],
    [/before (?:you (?:ever )?see (?:a|the|your) (?:next )?preview|the next preview)/i, 'review before the preview address'],
    [/our team is getting started/i, 'a team starts on it by hand'],
    [/personal review/i, 'a personal review'],
  ];

  const senders: Array<[string, () => Promise<void>]> = [
    ['sendWelcomeEmail', () => sendWelcomeEmail('a@example.com', 'A')],
    ['sendAppLiveEmail', () => sendAppLiveEmail('a@example.com', 'A', 'Site', 'https://site.example.com', 'b-1')],
    ['sendPreviewReadyEmail', () => sendPreviewReadyEmail('a@example.com', 'A', 'Site', 'https://site.example.com', 'b-1')],
    ['sendBuildQueuedEmail', () => sendBuildQueuedEmail('a@example.com', 'A', 'Site')],
    ['sendHarborMadePublicEmail', () => sendHarborMadePublicEmail({ to: 'a@example.com', name: 'A', appName: 'Site', publicUrl: 'https://site.example.com', monthlyAmountCents: 2900 })],
    ['sendReviewReadyEmail (next-preview sentence)', () => sendReviewReadyEmail('a@example.com', 'Site', 'b-1')],
    ['sendReviewChangesRequestedEmail (next-preview sentence)', () => sendReviewChangesRequestedEmail('a@example.com', 'Site', 'b-1', 'Fix the footer.')],
  ];

  it('the patterns catch the sentences they replaced (control)', () => {
    const samples = [
      'Every website we deliver is co-created with you and a Senior Web Craftsman who personally reviews every page before it goes live.',
      'It has been built, tested, and personally reviewed by a Senior Web Craftsman.',
      'Ocean personally reviews every page before you ever see a preview URL.',
      'Your website has been queued and our team is getting started on it.',
      'We will rebuild and review again before you see the next preview.',
      'Kick off a free revision, we will rebuild and review again before the next preview.',
      'and a personal review by a Senior Web Craftsman before it goes live',
    ];
    for (const sample of samples) {
      expect(CLAIMS.some(([pattern]) => pattern.test(sample)), sample).toBe(true);
    }
  });

  for (const [name, send] of senders) {
    // The review-gate emails state a person's verdict on purpose; only the
    // sentence about the next preview is checked there.
    const reviewGate = name.startsWith('sendReview');
    it(`${name} makes no claim that a person builds the site or reviews it before the preview address`, async () => {
      const html = await htmlOf(send);
      expect(html.length).toBeGreaterThan(200);
      for (const [pattern, label] of CLAIMS) {
        if (reviewGate && label !== 'review before the preview address') continue;
        expect(pattern.test(html), `${name}: ${label}`).toBe(false);
      }
    });
  }

  it('the preview-ready email says the site is built and tested, and needs a recorded review approval before it goes live', async () => {
    const html = await htmlOf(() => sendPreviewReadyEmail('a@example.com', 'A', 'Site', 'https://site.example.com', 'b-1'));
    expect(html).toContain('has been built and tested.');
    expect(html).toContain('it needs a recorded review approval before it goes live');
    expect(html).toContain('email you the new preview address, and it will need a recorded review approval again before it goes live');
  });

  it('the queued email says the preview address arrives by email and every website needs a recorded review approval before it goes live', async () => {
    const html = await htmlOf(() => sendBuildQueuedEmail('a@example.com', 'A', 'Site'));
    expect(html).toContain('library of tested building blocks');
    expect(html).toContain('staged at a preview address that we email to you');
    expect(html).toContain('Every website needs a recorded review approval before it goes live.');
  });

  it('the how-it-works email puts the preview before the review, and the review before going live', async () => {
    const { sendWelcomeHowItWorksEmail } = await import('../../src/services/email');
    const html = await htmlOf(() => sendWelcomeHowItWorksEmail('a@example.com', 'A'));
    const preview = html.indexOf('You walk around your preview');
    const review = html.indexOf('Every website needs a recorded review approval before it goes live');
    const publish = html.indexOf('you make it public');
    expect(preview).toBeGreaterThan(0);
    expect(review).toBeGreaterThan(preview);
    expect(publish).toBeGreaterThan(review);
    expect(html).not.toMatch(/personally/i);
  });

  it('the use-cases email promises a review, not a personal one by a named role', async () => {
    const { sendWelcomeUseCasesEmail } = await import('../../src/services/email');
    const html = await htmlOf(() => sendWelcomeUseCasesEmail('a@example.com', 'A'));
    expect(html).toContain('a recorded review approval before it goes live');
    // (The email shell's byline names the founder and his title; that is not a claim about the review.)
    expect(html).not.toMatch(/Senior Web Craftsman (?:reviews|reviewed)|personal review/);
  });

  it('the post-payment email prices the subscription with a recorded review approval for every website, not a named reviewer', async () => {
    const html = await htmlOf(() =>
      sendHarborMadePublicEmail({ to: 'a@example.com', name: 'A', appName: 'Site', publicUrl: 'https://site.example.com', monthlyAmountCents: 2900 })
    );
    expect(html).toContain('a recorded review approval for every website before it goes live');
  });
});


describe('sendPaymentNotAppliedEmail (the operator is told a paid session was not applied)', () => {
  const alert = {
    reason: 'the account already has a keeper with a subscription that this payment may not replace',
    sessionId: 'cs_alert_1',
    stripeSubscriptionId: 'sub_alert_1',
    buildId: 'build-alert-1',
    customerId: 'customer-alert-1',
    trialId: 'trial-alert-1',
    monthlyAmountCents: 2900,
  };
  let savedAdmin: string | undefined;
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    savedAdmin = process.env.ADMIN_EMAIL;
    delete process.env.RESEND_API_KEY;
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
    if (savedAdmin === undefined) delete process.env.ADMIN_EMAIL;
    else process.env.ADMIN_EMAIL = savedAdmin;
  });

  it('does nothing without ADMIN_EMAIL, and the warning carries the ids so the payment is still in the log', async () => {
    delete process.env.ADMIN_EMAIL;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    await sendPaymentNotAppliedEmail(alert);
    expect(mockQuery).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('ADMIN_EMAIL not set'), expect.stringContaining('cs_alert_1'));
    warn.mockRestore();
  });

  it('goes to the operator only, as a transactional alert with no unsubscribe link, and names no customer address', async () => {
    process.env.ADMIN_EMAIL = 'operator@example.test';
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'resend-1' }) });
    vi.stubGlobal('fetch', mockFetch);

    await sendPaymentNotAppliedEmail(alert);

    const [, init] = mockFetch.mock.calls[0];
    const sent = JSON.parse(init.body);
    expect(sent.to).toEqual(['operator@example.test']);
    expect(sent.subject).toContain('cs_alert_1');
    expect(sent.html).toContain('sub_alert_1');
    expect(sent.html).toContain('$29.00');
    expect(sent.html).toContain('may not replace');
    expect(sent.html).not.toMatch(/Unsubscribe/);
    expect(sent.headers).toBeUndefined();
    // Logged as the alert, with the ids a person looks the payment up by.
    const logCall = mockQuery.mock.calls.find(([sql]) => /INSERT INTO email_log/.test(String(sql)));
    expect(logCall![1][0]).toBe('harbor_payment_not_applied');
    expect(JSON.parse(logCall![1][8])).toMatchObject({ stripe_session_id: 'cs_alert_1', build_id: 'build-alert-1', customer_id: 'customer-alert-1', trial_id: 'trial-alert-1' });
  });

  it('says plainly when nothing could be recorded against an account, and cuts an id that is longer than any real one', async () => {
    process.env.ADMIN_EMAIL = 'operator@example.test';
    process.env.RESEND_API_KEY = 'live-key-xyz';
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'resend-2' }) });
    vi.stubGlobal('fetch', mockFetch);

    await sendPaymentNotAppliedEmail({ ...alert, customerId: null, trialId: null, sessionId: `cs_${'x'.repeat(5000)}<b>` });

    const sent = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(sent.html).toContain('Nothing could be recorded against an account');
    expect(sent.subject.length).toBeLessThan(200);
    expect(sent.html).not.toContain('x'.repeat(101));
    expect(sent.html).not.toContain('<b>');
  });
});
