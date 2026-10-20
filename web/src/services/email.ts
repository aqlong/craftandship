// web/src/services/email.ts
// Sends transactional email via Resend REST API using fetch (Node 18+ built-in).
// Every send attempt is logged to the email_log table regardless of outcome.

import crypto from 'crypto';
import pool from '../db';
import { getSessionSecret, isDevOrTestEnv } from '../middleware/auth';
import { redactSecretParams } from '../utils/recent-errors';

const RESEND_API_URL = 'https://api.resend.com/emails';
const RESEND_TIMEOUT_MS = 15_000;
const DEFAULT_FROM = 'Craft & Ship <noreply@craftandship.io>';

// Templates that are not marketing and must reach the recipient even after they
// unsubscribed from the rest: account security (password reset, verification)
// and operational alerts that go to ADMIN_EMAIL. They carry no unsubscribe link
// or List-Unsubscribe header and skip the unsubscribe check in sendEmail().
const TRANSACTIONAL_TEMPLATES = new Set([
  'password_reset',
  'email_verification',
  'harbor_crisis_alert',
  'harbor_persona_learning_digest',
  'harbor_payment_not_applied',
]);

// Absolute origin for every link and image inside an email. Email clients have
// no base URL, so a relative href ("/unsubscribe?...") is a dead link there.
// BASE_URL is documented in web/.env.example; the fallback is the production
// site this code shipped for.
function siteUrl(): string {
  return (process.env.BASE_URL || 'https://craftandship.io').replace(/\/+$/, '');
}

// The same escaping as escapeHtml in app.ts (tests/unit/email.escape-html.test.ts
// holds the two to one table): safe in element content and in an attribute
// quoted with either quote character.
function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Signed with the resolved session secret (middleware/auth.ts), never with a
// literal: a token signed with a value that ships in the repository could be
// forged by anyone, for any address.
function generateUnsubscribeToken(email: string): string {
  return crypto.createHmac('sha256', getSessionSecret()).update(email).digest('hex');
}

function unsubscribeUrlFor(email: string): string {
  return `${siteUrl()}/unsubscribe?email=${encodeURIComponent(email)}&token=${generateUnsubscribeToken(email)}`;
}

// email_log.html_body keeps the full HTML so /admin/emails can preview what was
// sent. Links in that HTML carry single-use secrets (password reset, email
// verification, magic login) and the unsubscribe token; storing them would let
// anyone who can read the table or the admin page act as the recipient, and
// would undo hashing those tokens at rest. Keep the link, drop the secret.
// An href is HTML, so its "&" may be written "&amp;", and a link may be nested
// inside another URL, where "?token=" becomes "%3Ftoken%3D"; the shared
// helper matches those spellings too.
function redactSecretsForLog(html: string): string {
  return redactSecretParams(html);
}

function emailShell(subject: string, body: string, unsubscribeUrl?: string): string {
  const unsubscribeLink = unsubscribeUrl
    ? `&nbsp;&middot;&nbsp;<a href="${unsubscribeUrl}" style="color:#8A7560;text-decoration:underline;">Unsubscribe</a>`
    : '';

  // Harbor v2 (2026-04-27): warm-paper letter aesthetic. Cormorant Garamond
  // for display + italic accents (with reliable serif fallback for clients
  // that strip Google Fonts), Source Sans 3 fallback for body (system sans
  // is fine when fonts don't load). Clay italic for the rhythm accent.
  // Light-only color-scheme so clients don't auto-invert the cream paper.
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="color-scheme" content="light" />
  <meta name="supported-color-schemes" content="light" />
  <title>${escapeHtml(subject)}</title>
  <style>
    body { margin: 0; padding: 0; background: #EAD8B6; font-family: 'Source Sans 3', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #3A2A1F; }
    .wrapper { max-width: 560px; margin: 40px auto; background: #FBF1DC; border-radius: 5px; overflow: hidden; box-shadow: 0 8px 22px rgba(58, 42, 31, 0.12), 18px 14px 30px rgba(58, 42, 31, 0.08); border: 1px solid rgba(58, 42, 31, 0.10); }
    .header { background: #F1E4C9; padding: 22px 32px 20px; border-bottom: 1px solid rgba(58, 42, 31, 0.12); }
    .header-mark { display: inline-flex; align-items: center; gap: 14px; }
    .header-logo { display: block; width: 44px; height: 44px; border-radius: 50%; flex-shrink: 0; }
    .header-text { display: flex; flex-direction: column; gap: 2px; }
    .header-name { font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, 'Times New Roman', serif; font-size: 1.5rem; font-weight: 500; color: #3A2A1F; letter-spacing: -0.005em; line-height: 1; }
    .header-name em { font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, serif; font-style: italic; font-weight: 400; color: #A56F4F; font-size: 1.65rem; margin-left: 2px; }
    .header-byline { font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, serif; font-style: italic; font-size: 0.8rem; color: #8A7560; letter-spacing: 0.01em; line-height: 1; }
    .body { padding: 36px 36px 32px; }
    .body p { margin: 0 0 1rem 0; line-height: 1.7; font-size: 1rem; color: #5A4636; }
    .body p:last-child { margin-bottom: 0; }
    .body a { color: #A56F4F; text-decoration: underline; text-underline-offset: 3px; }
    .body a:hover { color: #8E5A1F; }
    .body strong { color: #3A2A1F; font-weight: 600; }
    .body em { font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, serif; font-style: italic; color: #A56F4F; font-weight: 400; }
    .body h1, .body h2, .body h3 { font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, serif; font-weight: 500; color: #3A2A1F; letter-spacing: -0.01em; line-height: 1.15; margin: 28px 0 14px; }
    .body h1 { font-size: 2rem; }
    .body h2 { font-size: 1.5rem; }
    .body h3 { font-size: 1.25rem; }
    .body ul, .body ol { color: #5A4636; line-height: 1.75; padding-left: 1.25rem; }
    .body ul li, .body ol li { margin-bottom: 0.35rem; }
    .body blockquote { margin: 18px 0; padding: 16px 20px; border-left: 2px solid #A56F4F; background: rgba(165, 111, 79, 0.06); color: #3A2A1F; line-height: 1.65; font-size: 1rem; font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, serif; font-style: italic; border-radius: 5px; }
    .button { display: inline-block; background: #3A2A1F; color: #FBF1DC !important; padding: 11px 22px; font-size: 14px; font-weight: 600; letter-spacing: 0.02em; border-radius: 5px; text-decoration: none !important; box-shadow: 0 6px 14px rgba(58, 42, 31, 0.18); }
    .signature { margin-top: 28px; padding-top: 22px; border-top: 1px dashed rgba(58, 42, 31, 0.16); font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, serif; font-style: italic; font-size: 1.05rem; color: #5A4636; }
    .footer { background: #EAD8B6; padding: 20px 32px; text-align: center; font-size: 0.75rem; color: #8A7560; border-top: 1px solid rgba(58, 42, 31, 0.08); font-family: 'Cormorant Garamond', 'EB Garamond', Georgia, serif; font-style: italic; letter-spacing: 0.01em; }
    .footer a { color: #8A7560; }
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <div class="header-mark">
        <img src="${siteUrl()}/brand-harbor/lighthouse-logo-96.png" alt="Craft &amp; Ship lighthouse mark" class="header-logo" />
        <div class="header-text">
          <span class="header-name">Craft &amp; <em>Ship</em></span>
          <span class="header-byline">Ocean Longnion &middot; Senior Web Craftsman &middot; Austin, TX</span>
        </div>
      </div>
    </div>
    <div class="body">
      ${body}
    </div>
    <div class="footer">Craft &amp; Ship &middot; Tended from Austin, Texas${unsubscribeLink}</div>
  </div>
</body>
</html>`;
}

function getConfig(): { apiKey: string; fromEmail: string } {
  return {
    apiKey: process.env.RESEND_API_KEY ?? '',
    fromEmail: process.env.RESEND_FROM_EMAIL ?? DEFAULT_FROM,
  };
}

interface EmailLogParams {
  template: string;
  toEmail: string;
  subject: string;
  status: 'sent' | 'failed' | 'skipped';
  statusCode?: number | null;
  errorMessage?: string | null;
  resendId?: string | null;
  durationMs: number;
  metadata?: Record<string, string> | null;
  htmlBody?: string | null;
}

async function logEmail(params: EmailLogParams): Promise<void> {
  try {
    await pool.query(
      `INSERT INTO email_log
        (template, to_email, subject, status, status_code, error_message, resend_id, duration_ms, metadata, html_body)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
      [
        params.template,
        params.toEmail,
        params.subject,
        params.status,
        params.statusCode ?? null,
        params.errorMessage ?? null,
        params.resendId ?? null,
        params.durationMs,
        params.metadata ? JSON.stringify(params.metadata) : null,
        params.htmlBody ?? null,
      ]
    );
  } catch (err) {
    console.error('[email] Failed to write email_log row:', (err as Error).message);
  }
}

async function isUnsubscribed(email: string): Promise<boolean> {
  try {
    const result = await pool.query(
      'SELECT email_unsubscribed FROM customers WHERE email = $1',
      [email]
    );
    if (result.rows.length === 0) return false;
    return result.rows[0].email_unsubscribed === true;
  } catch (err) {
    console.error('[email] Failed to check unsubscribe status:', (err as Error).message);
    return false;
  }
}

async function sendEmail(params: {
  to: string;
  subject: string;
  html: string;
  template: string;
  metadata?: Record<string, string>;
}): Promise<void> {
  const { apiKey, fromEmail } = getConfig();
  const startTime = Date.now();

  const transactional = TRANSACTIONAL_TEMPLATES.has(params.template);
  const loggedHtml = redactSecretsForLog(params.html);

  // Check unsubscribe status before sending (marketing and lifecycle mail only)
  const unsubscribed = transactional ? false : await isUnsubscribed(params.to);
  if (unsubscribed) {
    console.info(`[email] Skipping send to ${params.to}: unsubscribed`);
    await logEmail({
      template: params.template,
      toEmail: params.to,
      subject: params.subject,
      status: 'skipped',
      errorMessage: 'unsubscribed',
      durationMs: 0,
      metadata: params.metadata ?? null,
      htmlBody: loggedHtml,
    });
    return;
  }

  if (!apiKey) {
    console.warn(
      `[email] RESEND_API_KEY not set; skipping send to ${params.to}, subject: ${params.subject}`
    );
    await logEmail({
      template: params.template,
      toEmail: params.to,
      subject: params.subject,
      status: 'skipped',
      errorMessage: 'RESEND_API_KEY not set',
      durationMs: 0,
      metadata: params.metadata ?? null,
      htmlBody: loggedHtml,
    });
    return;
  }

  try {
    const response = await fetch(RESEND_API_URL, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from: fromEmail,
        to: [params.to],
        subject: params.subject,
        html: params.html,
        reply_to: ['support@craftandship.io'],
        headers: transactional ? undefined : {
          'List-Unsubscribe': `<${unsubscribeUrlFor(params.to)}>`,
        },
      }),
      signal: AbortSignal.timeout(RESEND_TIMEOUT_MS),
    });

    const durationMs = Date.now() - startTime;

    if (!response.ok) {
      const body = await response.text().catch(() => '(no body)');
      console.error(`[email] Resend API error ${response.status}: ${body}`);
      await logEmail({
        template: params.template,
        toEmail: params.to,
        subject: params.subject,
        status: 'failed',
        statusCode: response.status,
        errorMessage: body,
        resendId: null,
        durationMs,
        metadata: params.metadata ?? null,
        htmlBody: loggedHtml,
      });
    } else {
      const json = await response.json().catch(() => ({})) as { id?: string };
      console.info(`[email] Sent '${params.subject}' to ${params.to}`);
      await logEmail({
        template: params.template,
        toEmail: params.to,
        subject: params.subject,
        status: 'sent',
        statusCode: response.status,
        resendId: json.id ?? null,
        durationMs,
        metadata: params.metadata ?? null,
        htmlBody: loggedHtml,
      });
    }
  } catch (err) {
    const durationMs = Date.now() - startTime;
    console.error(
      `[email] Failed to send '${params.subject}' to ${params.to}:`,
      (err as Error).message
    );
    await logEmail({
      template: params.template,
      toEmail: params.to,
      subject: params.subject,
      status: 'failed',
      errorMessage: (err as Error).message,
      durationMs,
      metadata: params.metadata ?? null,
      htmlBody: loggedHtml,
    });
  }
}

export { generateUnsubscribeToken, escapeHtml };

export async function sendWelcomeEmail(to: string, name: string): Promise<void> {
  const subject = 'Welcome to Craft & Ship';
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Welcome to Craft & Ship. Every website we deliver takes shape in conversation with you, is built from a library of tested building blocks, and needs a recorded review approval before it goes live.</p>
<p>You can start your first build by visiting your dashboard and describing the website you want us to create.</p>
<p>If you have any questions, just reply to this email. We would love to hear from you.</p>
<p>The Craft & Ship Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'welcome' });
}

export async function sendAppLiveEmail(
  to: string,
  name: string,
  appName: string,
  liveUrl: string,
  buildId?: string
): Promise<void> {
  const shortId = buildId ? buildId.slice(0, 8) : '';
  const subject = shortId ? `Your website is live: ${appName} [${shortId}]` : `Your website is live: ${appName}`;
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Great news. Your website <strong>${escapeHtml(appName)}</strong> is now live and ready to use. It has been built, tested, reviewed, and approved by you.</p>
<p>You can access it here: <a href="${escapeHtml(liveUrl)}">${escapeHtml(liveUrl)}</a></p>
<p>Two quick questions to help us improve:</p>
<ul style="color:#5A4636;line-height:1.75;">
  <li><strong>What questions did you have</strong> that the site didn't answer before you decided to build?</li>
  <li><strong>Who else do you know</strong> that could use something like this?</li>
</ul>
<p>Just reply to this email with your thoughts. It takes 30 seconds and helps us build a better product.</p>
<p>The Craft & Ship Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'app_live' });
}

export async function sendPreviewReadyEmail(
  to: string,
  name: string,
  appName: string,
  previewUrl: string,
  buildId: string,
  adminEmail?: string,
  magicLinkUrl?: string
): Promise<void> {
  const shortId = buildId ? buildId.slice(0, 8) : '';
  const subject = shortId ? `Your website is ready for review: ${appName} [${shortId}]` : `Your website is ready for review: ${appName}`;
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const statusUrl = `${siteUrl()}/builds/${buildId}`;
  const loginEmail = escapeHtml(adminEmail || to);
  const loginSection = magicLinkUrl
    ? `<p style="margin-top:1.2rem;"><strong>Login to your website:</strong></p>
<p><a href="${escapeHtml(magicLinkUrl)}" style="display:inline-block;background:#3A2A1F;color:#fff;padding:12px 24px;border-radius:5px;text-decoration:none;font-weight:600;">Log In to Your Website</a></p>
<p style="font-size:0.85rem;color:#7E6E55;margin-top:8px;">This link expires in 24 hours. After logging in, you will set your own password.</p>
<p style="font-size:0.85rem;color:#7E6E55;">Admin account: ${loginEmail}</p>`
    : `<p style="margin-top:1.2rem;"><strong>Login:</strong></p>
<p style="font-size:0.85rem;color:#7E6E55;">Login instructions have been sent separately. Admin account: ${loginEmail}</p>`;
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Your website <strong>${escapeHtml(appName)}</strong> has been built and tested. It's now staged at a preview URL, and it needs a recorded review approval before it goes live.</p>
<p>Take a look and make sure everything is how you want it: <a href="${escapeHtml(previewUrl)}">${escapeHtml(previewUrl)}</a></p>
${loginSection}
<p>Your website needs a recorded review approval before it can go live. When the review is done you will get another email, and your <a href="${escapeHtml(statusUrl)}">build status page</a> will then offer <strong>Approve and Go Live</strong>.</p>
<p>If anything needs changing, you can request modifications from the same page. We will rebuild it, email you the new preview address, and it will need a recorded review approval again before it goes live.</p>
<p>The Craft & Ship Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'preview_ready' });
}

export async function sendBuildFailedEmail(
  to: string,
  name: string,
  appName: string
): Promise<void> {
  const subject = `Build failed: ${appName}`;
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>We ran into an issue while building your website <strong>${escapeHtml(appName)}</strong> and the build did not complete.</p>
<p>Our team has been notified. If you have questions, just reply to this email.</p>
<p>The Craft & Ship Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'build_failed' });
}

export async function sendBuildQueuedEmail(
  to: string,
  name: string,
  appName: string
): Promise<void> {
  const subject = `Your build is in the queue: ${appName}`;
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Your website <strong>${escapeHtml(appName)}</strong> has been queued for building.</p>
<p>Here is what happens next: your website is built from a library of tested building blocks, tailored to your spec, then tested and staged at a preview address that we email to you. Every website needs a recorded review approval before it goes live.</p>
<p>You can check the progress on your <a href="${siteUrl()}/dashboard">dashboard</a>.</p>
<p>If you have any questions, just reply to this email.</p>
<p>The Craft & Ship Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'build_queued' });
}

/**
 * Development convenience. With no mail provider configured nothing is sent, so
 * a developer running the app locally could never follow the verification or
 * reset link. In development or test mode only (isDevOrTestEnv), and only while
 * RESEND_API_KEY is unset, the one-time link is printed to the server console.
 * It is never written to an HTTP response, a page or email_log (the logged HTML
 * has its tokens redacted), and production mode never prints it.
 */
export function logDevEmailLink(label: string, url: string): void {
  if (!isDevOrTestEnv()) return;
  if (process.env.RESEND_API_KEY) return;
  console.info(`[dev] Email is not configured. ${label}: ${url}`);
}

/**
 * `name` is the account's name when its owner has settled it (a verified
 * account), else null and the mail greets nobody by name: the name on an
 * unverified account was typed by whoever created it.
 */
export async function sendPasswordResetEmail(
  to: string,
  name: string | null,
  resetUrl: string
): Promise<void> {
  logDevEmailLink('Password reset link', resetUrl);
  const subject = 'Reset your Craft & Ship password';
  const body = `
<p>Hi ${name ? escapeHtml(name) : 'there'},</p>
<p>We received a request to reset your password. Click the link below to set a new one:</p>
<p><a href="${escapeHtml(resetUrl)}">${escapeHtml(resetUrl)}</a></p>
<p>This link will expire in 1 hour. If you did not request a password reset, you can safely ignore this email.</p>
<p>The Craft & Ship Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body), template: 'password_reset' });
}

/**
 * Send a crisis alert email to the harbor master (ADMIN_EMAIL).
 *
 * Fired when the companion runtime detects suicide, self-harm, abuse,
 * acute-medical or harm-to-others signals in keeper text via
 * `crisis_detection.ts`. This is the email-side of the Phase 2 § 5.5
 * "Crisis email plumbing" deliverable. Goes ONLY to the harbor master, not
 * the keeper; the keeper has already been served the deterministic crisis
 * script inline in their conversation that names AI status and points to
 * 988 and findahelpline.com, and to 911 for abuse, a medical emergency or a
 * threat to another person.
 *
 * Includes the categories that fired (suicide, self-harm, abuse,
 * acute-medical, harm-to-others),
 * the trial_id or keeper_id (so Ocean can find the conversation in
 * /admin/harbor → dev_trial deep-link), and the timestamp. Does NOT
 * include the keeper's verbatim text (privacy posture; Ocean reads
 * the conversation in the admin surface, not in their inbox).
 *
 * Fire-and-forget like other email sends. RESEND_API_KEY missing →
 * skipped + logged. ADMIN_EMAIL missing → no-op + warn.
 *
 * Note: this function does NOT add an unsubscribe link, admin alerts
 * are operational, not marketing, and bypass the unsubscribe check
 * by going to a hardcoded admin address.
 */
export async function sendCrisisAlertEmail(opts: {
  categories: string[];
  trialId?: string | null;
  keeperId?: string | null;
}): Promise<void> {
  const adminEmail = (process.env.ADMIN_EMAIL || '').trim();
  if (!adminEmail) {
    console.warn('[email/crisis] ADMIN_EMAIL not set; cannot send crisis alert', JSON.stringify(opts));
    return;
  }
  const cats = (opts.categories || []).slice(0, 8).map((c) => escapeHtml(String(c))).join(', ') || '(none)';
  const idLine = opts.trialId
    ? `Trial id: <code>${escapeHtml(opts.trialId)}</code>`
    : opts.keeperId
    ? `Keeper id: <code>${escapeHtml(opts.keeperId)}</code>`
    : '(no identity context)';
  const subject = `[CAS Harbor crisis] ${cats}`;
  const body = `
<p>Crisis-detection rail fired in a companion conversation.</p>
<p><strong>Categories:</strong> ${cats}</p>
<p>${idLine}</p>
<p>Timestamp: ${new Date().toISOString()}</p>
<p>The keeper has already received the deterministic crisis script in their conversation (988 / findahelpline.com / 911-on-abuse).</p>
<p>Review the conversation at <a href="${siteUrl()}/admin/harbor">${siteUrl()}/admin/harbor</a> (dev_trial deep-link).</p>
<p style="color:#7E6E55;font-size:0.85rem;">This is an operational alert; not subject to unsubscribe.</p>
`;
  await sendEmail({
    to: adminEmail,
    subject,
    html: emailShell(subject, body),
    template: 'harbor_crisis_alert',
    metadata: {
      categories: cats,
      trial_id: opts.trialId || '',
      keeper_id: opts.keeperId || '',
    },
  });
}

/** The longest id the payment alert prints or stores. */
const MAX_ALERT_ID_CHARS = 100;

/**
 * Tell the operator (ADMIN_EMAIL) that a Stripe Checkout session was paid and
 * the payment could not be applied: the webhook left the build, the trial and
 * the keeper row as they were (routes/webhooks.ts, `reportUnappliedPayment`).
 * The money is real and nothing here refunds it; the mail carries the ids a
 * person needs to decide between refunding it in Stripe and applying it
 * themselves. It names no customer address and none of the conversation.
 *
 * Operational alert: no unsubscribe link, ADMIN_EMAIL only. ADMIN_EMAIL
 * missing is a no-op with a warning that carries the same ids, so the
 * payment is still in the log.
 */
export async function sendPaymentNotAppliedEmail(opts: {
  reason: string;
  sessionId: string;
  stripeSubscriptionId: string;
  buildId: string;
  customerId: string | null;
  trialId: string | null;
  monthlyAmountCents: number;
}): Promise<void> {
  const adminEmail = (process.env.ADMIN_EMAIL || '').trim();
  if (!adminEmail) {
    console.warn('[email/payment] ADMIN_EMAIL not set; cannot send the payment alert', JSON.stringify(opts));
    return;
  }
  // 0 means the session carried no amount the webhook could read; the line is then left out.
  const dollars = opts.monthlyAmountCents > 0 ? `$${(opts.monthlyAmountCents / 100).toFixed(2)}` : null;
  // The ids come from a signed Stripe event; none is longer than Stripe makes
  // one, but the subject is stored in a VARCHAR(500) column, so each is cut.
  const id = (value: string | null): string | null => (value === null ? null : value.slice(0, MAX_ALERT_ID_CHARS));
  const sessionId = id(opts.sessionId) as string;
  // The webhook passes the account only when it recorded the subscription against it.
  const recorded = opts.customerId
    ? "The subscription is recorded against the account, so Stripe's later events for it are accepted."
    : "Nothing could be recorded against an account, so Stripe's later events for this subscription find no record: they are retried for a day and then dropped.";
  const subject = `[CAS Harbor payment] a paid session was not applied (${sessionId})`;
  const line = (label: string, value: string | null): string =>
    value ? `<li>${escapeHtml(label)}: <code>${escapeHtml(value)}</code></li>` : '';
  const body = `
<p>A Stripe Checkout session was paid, and the payment could not be applied.</p>
<p><strong>Why:</strong> ${escapeHtml(opts.reason)}.</p>
<ul>
${line('Checkout session', sessionId)}
${line('Subscription', id(opts.stripeSubscriptionId))}
${line('Monthly amount', dollars)}
${line('Build', id(opts.buildId))}
${line('Account (customer id)', id(opts.customerId))}
${line('Trial', id(opts.trialId))}
</ul>
<p>The money is real and was not refunded. ${recorded} Decide in Stripe whether to refund it or cancel it, or apply it yourself.</p>
<p style="color:#7E6E55;font-size:0.85rem;">This is an operational alert; not subject to unsubscribe.</p>
`;
  await sendEmail({
    to: adminEmail,
    subject,
    html: emailShell(subject, body),
    template: 'harbor_payment_not_applied',
    metadata: {
      stripe_session_id: sessionId,
      stripe_subscription_id: id(opts.stripeSubscriptionId) as string,
      build_id: id(opts.buildId) as string,
      customer_id: id(opts.customerId) || '',
      trial_id: id(opts.trialId) || '',
    },
  });
}

/**
 * Send a weekly Haven persona learning digest to the harbor master
 * (ADMIN_EMAIL). The body is composed externally (see
 * `cron/persona_learning_digest.ts:formatDigestBody`) as plain text;
 * this helper wraps it in a `<pre>` block inside the existing HTML
 * shell so the bullet structure survives in any client and the
 * monospace render makes the agent_learnings rows easy to scan.
 *
 * Operational alert: no unsubscribe link, ADMIN_EMAIL only. Like
 * `sendCrisisAlertEmail`, RESEND_API_KEY missing → skipped + logged
 * via the underlying `sendEmail`; ADMIN_EMAIL missing → no-op + warn
 * (the digest is wasted without a recipient).
 */
export async function sendPersonaLearningDigestEmail(opts: {
  subject: string;
  body: string;
}): Promise<void> {
  const adminEmail = (process.env.ADMIN_EMAIL || '').trim();
  if (!adminEmail) {
    console.warn(
      '[email/persona-digest] ADMIN_EMAIL not set; cannot send persona learning digest'
    );
    return;
  }
  const subject = opts.subject;
  const html = `
<p>Weekly Haven persona learning digest.</p>
<pre style="font-family:'SFMono-Regular',Menlo,Consolas,monospace;font-size:0.85rem;line-height:1.5;color:#3A2A1F;background:rgba(58,42,31,0.04);padding:16px;border-radius:5px;white-space:pre-wrap;word-break:break-word;">${escapeHtml(opts.body)}</pre>
<p style="color:#7E6E55;font-size:0.85rem;">This is an operational alert; not subject to unsubscribe.</p>
`;
  await sendEmail({
    to: adminEmail,
    subject,
    html: emailShell(subject, html),
    template: 'harbor_persona_learning_digest',
  });
}

/**
 * Sent after a keeper presses "Make this public" and Stripe completes the
 * subscription Checkout. The website is now live on its public URL; the
 * trial cookie identity has been promoted to a keepers row; the next time
 * the keeper opens /lighthouse/table they see Haven's blessing turn.
 *
 * Includes the monthly amount the keeper chose so the post-payment email
 * confirms what they're paying. Pass cents (e.g. 3900) and the helper
 * formats as $39/mo. Omit to skip the line entirely (older callers).
 *
 * This is a customer-facing email; it carries the unsubscribe link.
 * Brand-voice rules apply (no em dashes, "website" not "site/app",
 * affirmative voice, calm).
 */
export async function sendHarborMadePublicEmail(opts: {
  to: string;
  name: string;
  appName: string;
  publicUrl: string;
  buildId?: string;
  monthlyAmountCents?: number;
}): Promise<void> {
  const { to, name, appName, publicUrl, buildId, monthlyAmountCents } = opts;
  const shortId = buildId ? buildId.slice(0, 8) : '';
  const subject = shortId
    ? `Your website is public: ${appName} [${shortId}]`
    : `Your website is public: ${appName}`;
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const tableUrl = `${siteUrl()}/lighthouse/table`;
  const amountLine = (typeof monthlyAmountCents === 'number' && monthlyAmountCents > 0)
    ? `<p>The subscription is in effect: <strong>$${(monthlyAmountCents / 100).toFixed(0)} a month.</strong> Includes hosting, unlimited revisions and rebuilds, and a recorded review approval for every website before it goes live. You can cancel from your website's page, linked from your conversation, whenever it feels right, or write to us to pause; your website stays up either way.</p>`
    : `<p>The subscription is in effect. You can cancel from your website's page, linked from your conversation, whenever it feels right, or write to us to pause; your website stays up either way.</p>`;
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Your website <strong>${escapeHtml(appName)}</strong> is public.</p>
<p>Anyone you share this URL with can read it now:</p>
<p style="text-align:center;margin:20px 0;">
  <a href="${escapeHtml(publicUrl)}" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">Visit your website</a>
</p>
<p style="font-size:0.95rem;color:#5A4636;">${escapeHtml(publicUrl)}</p>
<p>Haven left a small note for you. You'll see it next time you open <a href="${tableUrl}">your conversation</a>.</p>
${amountLine}
<p>The Craft &amp; Ship Team</p>
`;
  await sendEmail({
    to,
    subject,
    html: emailShell(subject, body, unsubscribeUrl),
    template: 'harbor_made_public',
    metadata: { build_id: buildId || '' },
  });
}


/**
 * Delivery email for a site that was built free, passed an automated review and
 * was published. Nothing in this repository calls it: its caller was an
 * automated review loop that ran outside this repository and is not included
 * here (see the review_status comments in db/schema.sql). The function is kept
 * as the record of what that path sent. The message says the site is free and
 * the customer's own, and invites (but does not ask for) a "name your price"
 * contribution by reply; it carries no payment link.
 */
export async function sendHarborLiveEmail(opts: {
  to: string;
  name: string;
  appName: string;
  liveUrl: string;
  buildId?: string;
}): Promise<void> {
  const { to, name, appName, liveUrl, buildId } = opts;
  const shortId = buildId ? buildId.slice(0, 8) : '';
  const subject = shortId
    ? `Your website is live: ${appName} [${shortId}]`
    : `Your website is live: ${appName}`;
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const tableUrl = `${siteUrl()}/lighthouse/table`;
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Your website <strong>${escapeHtml(appName)}</strong> is live, and it is yours. We built it from our conversation, looked it over carefully, and it is ready for the world.</p>
<p style="text-align:center;margin:22px 0;">
  <a href="${escapeHtml(liveUrl)}" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">Visit your website</a>
</p>
<p style="font-size:0.95rem;color:#5A4636;">${escapeHtml(liveUrl)}</p>
<p>Hosting and care are on us. If you would like a change, just open <a href="${tableUrl}">your conversation</a> and tell us; we will tend to it.</p>
<p style="color:#5A4636;">There is no bill for this. If Craft &amp; Ship is something you would like to support, you can name your price whenever it feels right; just reply to this note or mention it in your conversation. Either way, your website stays up.</p>
<p>The Craft &amp; Ship Team</p>
`;
  await sendEmail({
    to,
    subject,
    html: emailShell(subject, body, unsubscribeUrl),
    template: 'harbor_live',
    metadata: { build_id: buildId || '' },
  });
}

export async function sendAbandonNudgeEmail(
  to: string,
  name: string,
  resumeUrl: string
): Promise<void> {
  const subject = 'Your website idea is waiting for you';
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi <strong>${escapeHtml(name)}</strong>,</p>
<p>We noticed you started describing your website but didn't finish.</p>
<p>Your conversation is still saved and ready to pick up where you left off.</p>
<p style="text-align:center;margin:28px 0;">
  <a href="${escapeHtml(resumeUrl)}" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">Resume Your Project</a>
</p>
<p style="color:#7E6E55;font-size:0.85rem;">If you're no longer interested, no action needed; we'll clean things up automatically.</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'abandon_nudge' });
}

// --- Welcome nurture sequence templates ---

export async function sendWelcomeHowItWorksEmail(to: string, name: string): Promise<void> {
  const subject = 'Here is what happens when you build with Craft & Ship';
  const unsubscribeUrl = unsubscribeUrlFor(to);
  // Steps follow the process described in services/support-knowledge.ts.
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Yesterday you signed up. Here is what the process looks like:</p>
<p><strong>Step 1:</strong> You talk with Haven, our AI companion, about your work in plain English<br/>
<strong>Step 2:</strong> When the picture is clear, your website is built, free, in a preview<br/>
<strong>Step 3:</strong> You walk around your preview and ask for any changes<br/>
<strong>Step 4:</strong> Every website needs a recorded review approval before it goes live<br/>
<strong>Step 5:</strong> When you are happy, you make it public and pick the monthly amount that fits your practice<br/>
<strong>Step 6:</strong> Your website goes live</p>
<p>Revisions and rebuilds are unlimited.</p>
<p style="text-align:center;margin:28px 0;">
  <a href="${siteUrl()}/intake" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">Start Building</a>
</p>
<p>Ocean, Founder</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'welcome_how_it_works' });
}

export async function sendWelcomeUseCasesEmail(to: string, name: string): Promise<void> {
  const subject = 'Here is what other people are building';
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>People use Craft & Ship to build:</p>
<ul style="color:#5A4636;line-height:1.75;">
  <li>Appointment booking websites</li>
  <li>Job tracking dashboards</li>
  <li>Client portals with login</li>
  <li>Inventory management tools</li>
  <li>Simple CRMs</li>
</ul>
<p>Every website comes with login, a database, an admin panel, mobile-responsive design, and a recorded review approval before it goes live.</p>
<p style="text-align:center;margin:28px 0;">
  <a href="${siteUrl()}/intake" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">What Would You Build?</a>
</p>
<p>Ocean, Founder</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'welcome_use_cases' });
}

// The function and template names ('welcome_beta_urgency') are historical: this
// email, sent seven days after verification, used to push a limited-time beta
// price. email_schedule rows and the scheduler's template map still use that
// name, so it stays, but the copy now describes the real offer (see
// services/support-knowledge.ts).
export async function sendWelcomeBetaUrgencyEmail(to: string, name: string): Promise<void> {
  const subject = 'Your website conversation is waiting';
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>A quick note: your first build is free in preview. You only pay when you say "make it public", and you choose the monthly amount that fits your practice. Revisions and rebuilds are unlimited, and every website needs a recorded review approval before it goes live.</p>
<p>Whenever you are ready, pick the conversation back up.</p>
<p style="text-align:center;margin:28px 0;">
  <a href="${siteUrl()}/intake" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">Build Your Website Now</a>
</p>
<p>Ocean, Founder</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'welcome_beta_urgency' });
}

// --- Post-build follow-up templates ---

export async function sendPostBuildCheckInEmail(
  to: string,
  name: string,
  appName: string,
  liveUrl: string
): Promise<void> {
  const subject = `How is ${appName} working?`;
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Your <strong>${escapeHtml(appName)}</strong> has been live for a few days. A few things you can do:</p>
<ul style="color:#5A4636;line-height:1.75;">
  <li>Visit your website: <a href="${escapeHtml(liveUrl)}">${escapeHtml(liveUrl)}</a></li>
  <li>Request changes: use the "Request Revision" button on your dashboard</li>
  <li>Add features: request new features from your dashboard</li>
</ul>
<p>Everything is being monitored 24/7. If anything goes down, we fix it automatically.</p>
<p>How is it going? Reply with any feedback.</p>
<p>Ocean, Founder</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'post_build_check_in' });
}

export async function sendPostBuildTestimonialEmail(
  to: string,
  name: string,
  appName: string
): Promise<void> {
  const subject = 'Quick favor?';
  const unsubscribeUrl = unsubscribeUrlFor(to);
  const body = `
<p>Hi ${escapeHtml(name)},</p>
<p>Your <strong>${escapeHtml(appName)}</strong> has been running for a week. If it is working well, would you mind sharing a quick sentence about your experience? I would love to feature it on our site.</p>
<p>Just reply with a sentence or two. Or if something is not right, let me know and I will fix it.</p>
<p>Thanks,<br/>Ocean</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'post_build_testimonial' });
}

/**
 * Greets nobody by name: the mail goes to an address whose owner has proven
 * nothing yet, and any name there is was typed by whoever filled in the form,
 * who may be someone else.
 */
export async function sendVerificationEmail(
  to: string,
  verifyUrl: string
): Promise<void> {
  logDevEmailLink('Verification link', verifyUrl);
  const subject = 'Verify your Craft & Ship email address';
  const body = `
<p>Hi there,</p>
<p>Thanks for signing up! Please verify your email address to get started:</p>
<p><a href="${escapeHtml(verifyUrl)}" style="display:inline-block;background:#3A2A1F;color:#fff;padding:12px 24px;border-radius:5px;text-decoration:none;font-weight:600;">Verify Email Address</a></p>
<p>Or copy this link: <a href="${escapeHtml(verifyUrl)}">${escapeHtml(verifyUrl)}</a></p>
<p>This link expires in 24 hours. If you didn't create an account, you can safely ignore this email.</p>
<p>The Craft & Ship Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body), template: 'email_verification' });
}

// ---------------------------------------------------------------------------
// Review gate emails
// ---------------------------------------------------------------------------
// The admin review routes (routes/admin.ts) call these two functions when a
// verdict is recorded on a build at preview.

/**
 * Customer-facing email sent when a review verdict of "approved" is recorded
 * on the preview build (the admin approve route; it is not sent again for a
 * build that was already approved). From that moment the status page offers
 * "Approve and Go Live". What the button does depends on the build: it marks
 * the website live, or, for a website whose subscription has not started
 * (a Harbor build with no subscription; CUSTOMER_APPROVE_SQL in
 * routes/builds.ts), it leads to the page where the subscription starts, and
 * the payment marks it live. Going live changes a status; the address stays
 * the preview's. The email says all three.
 *
 * Fire-and-forget from the admin approve route handler; callers MUST wrap
 * the call with .catch(...) since sendEmail can still reject on Resend
 * transport errors even though it swallows the unsubscribe skip case.
 */
export async function sendReviewReadyEmail(
  customerEmail: string,
  appName: string,
  buildId: string
): Promise<void> {
  const subject = 'Your website has been reviewed and is ready to go live';
  const unsubscribeUrl = unsubscribeUrlFor(customerEmail);
  const statusUrl = `${siteUrl()}/builds/${buildId}`;
  const body = `
<p>Good news.</p>
<p>Your website <strong>${escapeHtml(appName)}</strong> has been reviewed and is ready to go live.</p>
<p>When you are ready, open your status page and click <strong>Approve and Go Live</strong>. If your website's subscription has not started yet, that button takes you to start it, and your website goes live when it does. Its address stays the one you have been previewing.</p>
<p style="text-align:center;margin:28px 0;">
  <a href="${escapeHtml(statusUrl)}" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">Open Status Page</a>
</p>
<p>If anything still feels off, you can request a revision from the same page. We will rebuild it, email you the new preview address, and it will need a recorded review approval again before it goes live.</p>
<p>The Craft &amp; Ship Team</p>
`;
  await sendEmail({
    to: customerEmail,
    subject,
    html: emailShell(subject, body, unsubscribeUrl),
    template: 'review_ready',
  });
}

/**
 * Customer-facing email sent when a review verdict of "changes requested" is recorded on the preview
 * build. The review notes are rendered inline (HTML-escaped) so the customer
 * can read them without clicking through, plus a CTA back to the status page
 * where the revision flow lives.
 *
 * The reviewNotes string is admin-entered content that flows into a customer
 * inbox, so it MUST be HTML-escaped (via escapeHtml) before interpolation.
 * The unit test "body contains review_notes HTML-escaped" is the XSS canary.
 *
 * Fire-and-forget from the admin request-changes route handler.
 */
export async function sendReviewChangesRequestedEmail(
  customerEmail: string,
  appName: string,
  buildId: string,
  reviewNotes: string
): Promise<void> {
  const subject = 'We have some suggestions for your website';
  const unsubscribeUrl = unsubscribeUrlFor(customerEmail);
  const statusUrl = `${siteUrl()}/builds/${buildId}`;
  const escapedNotes = escapeHtml(reviewNotes).replace(/\r?\n/g, '<br/>');
  const body = `
<p>The review of your website <strong>${escapeHtml(appName)}</strong> is finished and has a few suggestions before it goes live. Every website needs a recorded review approval before you are asked to approve it.</p>
<p><strong>Review notes:</strong></p>
<blockquote style="margin:16px 0;padding:16px 20px;border-left:2px solid rgba(1,135,160,0.5);background:rgba(1,135,160,0.04);color:#1A1A1E;line-height:1.65;font-size:0.95rem;border-radius:4px;">
  ${escapedNotes}
</blockquote>
<p>Open your status page to see the notes in context and kick off a free revision. We will rebuild it, email you the new preview address, and it will need a recorded review approval again before it goes live.</p>
<p style="text-align:center;margin:28px 0;">
  <a href="${escapeHtml(statusUrl)}" style="display:inline-block;padding:12px 28px;background:#3A2A1F;color:#fff;text-decoration:none;border-radius:5px;font-weight:600;">Open Status Page</a>
</p>
<p>If anything is unclear, just reply to this email.</p>
<p>The Craft &amp; Ship Team</p>
`;
  await sendEmail({
    to: customerEmail,
    subject,
    html: emailShell(subject, body, unsubscribeUrl),
    template: 'review_changes_requested',
  });
}
