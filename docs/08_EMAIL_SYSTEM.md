# Email System

## Overview

Craft & Ship sends email via the [Resend](https://resend.com) REST API using Node's built-in `fetch` (Node 18+). Web-tier mail goes through one function, `sendEmail()` in `web/src/services/email.ts`, and every attempt it makes, successful or not, is written to the `email_log` database table so the email can be audited, previewed, and debugged from the admin panel.

Two kinds of sender do not use `sendEmail()` or this log's redaction: the contact form (`POST /contact` in `web/src/app.ts` posts straight to Resend, to `ADMIN_EMAIL` only, and writes no `email_log` row; the page confirms with "Thank you" only when Resend answers 2xx, a provider error or network failure re-renders the form with the typed values and an error at HTTP 502, and a missing `RESEND_API_KEY` or `ADMIN_EMAIL` does the same at HTTP 503, naming the two variables only in development or test), and the Python services (`queue/mailer.py` for build lifecycle mail, which does check unsubscribe status and log to `email_log` but does not redact tokens; `coordinator/actuators.py`, `monitor/diagnose.py` and `ops/alerter.py` for operator alerts).

Key properties of every outbound email:
- **From:** `Craft & Ship <noreply@craftandship.io>` (overridable via `RESEND_FROM_EMAIL`)
- **Reply-To:** `support@craftandship.io`: replies from customers land in a shared inbox
- **Logging:** a row is inserted into `email_log` with status `sent`, `failed`, or `skipped`, plus the HTML body with the value of any `token=` query parameter replaced by `[redacted]` (`redactSecretsForLog`), so password-reset, verification, magic-login and unsubscribe links cannot be replayed from the table or the admin page
- **Unsubscribe header:** marketing and lifecycle mail carries a `List-Unsubscribe` header and an absolute footer link built from `BASE_URL`; transactional mail carries neither

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `RESEND_API_KEY` | Yes (production) | API key from the Resend dashboard |
| `RESEND_FROM_EMAIL` | No | Override the from address; defaults to `Craft & Ship <noreply@craftandship.io>` |
| `SESSION_SECRET` | Yes | Used as the HMAC key for generating deterministic unsubscribe tokens (the same secret that signs sessions; see `resolveSessionSecret`) |
| `BASE_URL` | Recommended | Public origin used for every link and image in an email, including the unsubscribe link; falls back to `https://craftandship.io` when unset, so set it on any other deployment |
| `RESEND_WEBHOOK_SECRET` | No | Signing secret for `POST /webhooks/resend` (delivery, bounce and complaint events); unset makes that endpoint reject every call |
| `ADMIN_EMAIL` | Yes | Recipient of the contact form and the operational alerts; no default |

If `RESEND_API_KEY` is not set, all emails are logged as `skipped` with `errorMessage = 'RESEND_API_KEY not set'`. This is the expected behavior in local development.

---

## Email Templates

Each template has a named sender helper in `web/src/services/email.ts`. The template name is what lands in `email_log.template`. **Transactional** templates (`TRANSACTIONAL_TEMPLATES`) skip the unsubscribe check, so a recipient who unsubscribed from marketing still gets them, and carry no unsubscribe link or `List-Unsubscribe` header.

| Template | Helper | Trigger | Transactional |
|---|---|---|---|
| `welcome` | `sendWelcomeEmail` | email verification succeeds (`GET /verify-email`) | no |
| `welcome_how_it_works`, `welcome_use_cases`, `welcome_beta_urgency` | `sendWelcome*Email` | the scheduled welcome sequence (`services/email-scheduler.ts`) | no |
| `email_verification` | `sendVerificationEmail` | `POST /signup` (for an address with no account or an unverified one), `POST /resend-verification` | yes |
| `password_reset` | `sendPasswordResetEmail` | `POST /forgot-password` for a known address | yes |
| `build_queued` | `sendBuildQueuedEmail` | a feature request is queued (`routes/features.ts`); the Stripe webhook no longer sends it | no |
| `preview_ready` | `sendPreviewReadyEmail` | the optional `POST /webhooks/build-status` hook with `status: 'preview'` and a `preview_url` (or the older `live_url`); the bundled worker does not call that hook and sends its own mail (`send_preview_ready` in `queue/mailer.py`) | no |
| `app_live` | `sendAppLiveEmail` | the build becomes live through `POST /builds/:buildId/approve`, which mails the build's owner even when the admin approves; the `build-status` hook rejects `live` with a 400 and sends nothing | no |
| `build_failed` | `sendBuildFailedEmail` | the optional `POST /webhooks/build-status` hook with `status: 'failed'` (a call that matches no build in an allowed prior status sends nothing); the bundled worker sends its own mail (`send_build_failed` in `queue/mailer.py`) | no |
| `review_ready`, `review_changes_requested` | `sendReviewReadyEmail`, `sendReviewChangesRequestedEmail` | the reviewer's decision at `/admin/reviews` | no |
| `harbor_made_public` | `sendHarborMadePublicEmail` | the make-public Stripe webhook (`handleHarborMakePublic` in `routes/webhooks.ts`); sent after the event's transaction commits, so it is lost if the process dies between the commit and the send | no |
| `harbor_payment_not_applied` | `sendPaymentNotAppliedEmail` | the make-public Stripe webhook, when a paid session cannot be applied (the account's keeper already has a subscription this payment may not replace, the trial is not the build's own, the build is not publishable, the build or the account cannot be found); sent after the event's transaction commits to `ADMIN_EMAIL` only, with the ids and no address, and it carries no unsubscribe link | yes |
| `harbor_live` | `sendHarborLiveEmail` | helper defined; no caller in this repository | no |
| `abandon_nudge` | `sendAbandonNudgeEmail` | `services/abandon-check.ts` | no |
| `post_build_check_in`, `post_build_testimonial` | `sendPostBuild*Email` | the scheduled post-build sequence | no |
| `harbor_crisis_alert` | `sendCrisisAlertEmail` | the companion's crisis detection fires; goes to `ADMIN_EMAIL` only. Sent after the visitor already has the crisis reply, and bounded by `crisisSideEffects` in `web/src/companion/runtime.ts`: one alert per identity per 30 minutes and 10 per hour per process (counters in process memory); past those limits the crisis is recorded in the server log only | yes |
| `harbor_persona_learning_digest` | `sendPersonaLearningDigestEmail` | the weekly persona-learning job; `ADMIN_EMAIL` only | yes |

The worker (`queue/mailer.py`) sends its own lifecycle mail directly and does not go through the web app: `send_preview_ready`, `send_build_failed`, `send_build_progress` and `send_review_pending` are the only functions it defines, and `queue/worker_pg.py` calls all four; see `docs/internals/worker.md`.

---

## `emailShell(subject, body, unsubscribeUrl?)`

Located in `web/src/services/email.ts`. Wraps any body string in a consistent HTML shell with:
- Header with the lighthouse mark and the Craft & Ship wordmark
- Padded body area
- Footer with "Craft & Ship, Tended from Austin, Texas"
- Optional unsubscribe link appended to the footer when `unsubscribeUrl` is provided

Parameters:
- `subject`: inserted into the `<title>` tag
- `body`: raw HTML string placed inside the body div
- `unsubscribeUrl` (optional): absolute URL for the unsubscribe link in the footer; helpers build it with `unsubscribeUrlFor(email)`, which prefixes `BASE_URL`

---

## `generateUnsubscribeToken(email)`

Generates a deterministic, unforgeable unsubscribe token for a given email address:

```
HMAC-SHA256(SESSION_SECRET, email) -> hex string (64 chars)
```

The token is stable: the same email always produces the same token for a given `SESSION_SECRET`. This means no token storage is required: the unsubscribe link carries the email, and the route re-derives the expected token for that address and compares it in constant time.

The secret comes from `getSessionSecret()` in `web/src/middleware/auth.ts`, never from a literal in `email.ts`: outside development and test the server will not start without a real `SESSION_SECRET` (at least 32 characters, at least 8 different ones, not a repeated block, no placeholder word; `sessionSecretWeakness`), and in development and test an unset one is replaced by a random per-process secret (so links from an earlier run stop working). A token signed with a value that ships in the repository could be forged for any address.

---

## Unsubscribe Flow

1. Every marketing and lifecycle email footer includes an absolute link: `{BASE_URL}/unsubscribe?email={email}&token={generateUnsubscribeToken(email)}` (the same URL is sent in the `List-Unsubscribe` header). Transactional emails carry no such link.
2. `GET /unsubscribe?email=...&token=...` (`web/src/routes/auth.ts`) lower-cases the email, recomputes the HMAC, and compares it with the provided token using `timingSafeEqualStrings`; it then looks up a customer with that email whose `email_unsubscribed = FALSE`.
3. On match, sets `email_unsubscribed = TRUE` on that customer row.
4. Renders `views/unsubscribe.ejs` with a success message.
5. Before every future send of a non-transactional template, `isUnsubscribed(email)` queries the `email_unsubscribed` column. If `TRUE`, the email is skipped and logged as `skipped` with `errorMessage = 'unsubscribed'`.

DB column: `customers.email_unsubscribed BOOLEAN DEFAULT FALSE`

---

## Password Reset Flow

1. Customer visits `/forgot-password` and submits their email.
2. `POST /forgot-password`: looks up the customer. Whether or not the email exists, the response is the same neutral message: "If that email exists, check your inbox for a reset link." This prevents user enumeration.
3. If the customer exists: generates a 32-byte random hex token via `crypto.randomBytes(32).toString('hex')`, inserts its SHA-256 hash into `password_reset_tokens` with a 1-hour expiry (the same statement prunes tokens that expired more than a day ago), and fires `sendPasswordResetEmail` (fire-and-forget). A successful reset also deletes the customer's rows in `user_sessions`, so every existing login is invalidated.
4. Customer clicks the link in their email: `GET /reset-password?token=...`
5. Server hashes the presented token and validates it: `WHERE token = $1 AND expires_at > NOW() AND used_at IS NULL`. Renders the form on success, or an error if invalid/expired.
6. Customer submits new password (min 8 chars): `POST /reset-password`
7. Server re-validates token, hashes the new password with bcrypt (12 rounds), and in one statement marks `password_reset_tokens.used_at = NOW()` (single-use), updates `customers.password` and sets `email_verified = TRUE`: a reset link went to the account's mailbox, so completing it proves the mailbox exactly as a verification link does. Outstanding reset and verification links for the account are then voided and the customer's sessions are deleted.
8. Renders a success confirmation. Customer can then log in with their new password.

DB table: `password_reset_tokens (id, customer_id, token, expires_at, used_at)`

---

## Email Verification Flow

Rule (the "Unverified accounts" comment block in `web/src/routes/auth.ts`): until the mailbox is proven, the password and name on an account are only a claim, and the claim that counts is the one made in the browser that proves the mailbox.

1. `POST /signup` keeps what was typed (password hash and name) in that browser's server-side session. For an address with no account, or an unverified one, it also writes it to the row and voids verification links issued for earlier claims; a verified account is never touched. The response is the same in all three cases, and the address is held in the session, never in a redirect URL (`/verify-email-sent` ignores `?email=`).
2. The 24-hour verification token is stored as a SHA-256 hash in `email_verification_tokens`. `GET /verify-email` spends the link, verifies the account and settles its password and name in one statement: to this browser's own claim when it has one for that address (the visitor is then logged in), otherwise to an unusable random password and a neutral name, and the visitor is sent to `/reset-password` to choose a password. Whatever an earlier claimant typed does not survive verification unless it was typed in the verifying browser.
3. A correct password at `POST /login` for an unverified account records the same claim before the "verify first" page, so someone who signed up on one device can finish on another.
4. The `welcome` email and the welcome sequence are scheduled when verification succeeds.

---

## Admin Email Log

URL: `/admin/emails`

Access: `requireAdmin` in `web/src/routes/admin.ts`, i.e. `isAdminSession`: a logged-in session established by a password login or email-verification link whose email equals `ADMIN_EMAIL`. With `ADMIN_EMAIL` unset nobody has access.

Features:
- Paginated table of all email log rows (newest first)
- Search by recipient email
- Status filter (sent / failed / skipped)
- Stats: sent/failed/skipped counts for today
- Click any row to open a detail page with the full HTML preview rendered in an iframe

The log stores: template name, recipient email, subject, status, HTTP status code from Resend, error message, Resend email ID, send duration (ms), JSON metadata, and the HTML body with `token=` values redacted (so a previewed reset or login link shows `[redacted]` in place of the secret).

---

## Cloudflare Email Routing

Inbound email for `craftandship.io` is routed via Cloudflare Email Routing: the support and general addresses forward to the operator inbox.

Reply-to on all outbound email is `support@craftandship.io`, so customer replies arrive in the operator inbox.

---

## How to Add a New Email Template

1. **Write the function** in `src/services/email.ts`:

```typescript
export async function sendMyNewEmail(to: string, name: string, ...): Promise<void> {
  const subject = 'Your subject line';
  const unsubscribeUrl = unsubscribeUrlFor(to);   // absolute, built from BASE_URL
  const body = `
<p>Hi ${name},</p>
<p>Your message here.</p>
<p>The CraftAndShip Team</p>
`;
  await sendEmail({ to, subject, html: emailShell(subject, body, unsubscribeUrl), template: 'my_new_template' });
}
```

2. **Export it** from the file. If the email is a security or operational message that must reach the recipient even after an unsubscribe, add its template name to `TRANSACTIONAL_TEMPLATES` and call `emailShell(subject, body)` without an unsubscribe URL.
3. **Import and call it** in the relevant route or service. Use fire-and-forget for non-blocking sends: `sendMyNewEmail(...).catch(err => console.error(...))`.
4. **Add a unit test** in `web/tests/unit/email.service.test.ts` covering the happy path and any edge cases.
5. **Verify in dev** by checking `/admin/emails` after triggering the flow; the email row should appear with `status = skipped` and `errorMessage = 'RESEND_API_KEY not set'`.

---

## Troubleshooting

**Domain not verified in Resend**
All sends will return a 4xx error. Check the Resend dashboard under Domains. Verify the DNS records (SPF, DKIM, DMARC) are correctly set in Cloudflare. Emails are logged as `failed` with the Resend error body.

**Emails logged as "skipped" for a user who should receive email**
Check the `email_unsubscribed` column: `SELECT email, email_unsubscribed FROM customers WHERE email = 'user@example.com';`. If `true`, the customer clicked unsubscribe. You can reset it manually: `UPDATE customers SET email_unsubscribed = FALSE WHERE email = '...'`.

**`RESEND_API_KEY not set` in skipped logs**
Expected in local development. Set the env var in your `.env` file and restart the server to test real sends.

**Wrong SESSION_SECRET between environments**
Unsubscribe tokens are derived from `SESSION_SECRET`. If the secret changes, existing unsubscribe links in sent emails will stop working (and so will every session and CSRF token). In development a missing secret is replaced by a random one at each start, which has the same effect. Do not rotate `SESSION_SECRET` without accepting that.
