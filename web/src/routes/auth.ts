import { Router, Request, Response } from 'express';
import bcrypt from 'bcrypt';
import crypto from 'crypto';
import pool from '../db';
import {
  requireAuth,
  generateCsrfToken,
  validateCsrfToken,
  safeReturnTo,
  timingSafeEqualStrings,
  SESSION_COOKIE_NAME,
  sessionCookieAttributes,
} from '../middleware/auth';
import { authRateLimit, signupRateLimit, accountDeleteRateLimit } from '../middleware/auth-rate-limit';
import { sendWelcomeEmail, sendPasswordResetEmail, sendVerificationEmail, generateUnsubscribeToken } from '../services/email';
import { scheduleWelcomeSequence } from '../services/email-scheduler';
import { claimAnonDraftBySessionId } from '../services/anon-intake';
import { cancelHarborSubscriptionAtPeriodEnd } from '../services/stripe';
import type { AnonSpecPreview } from '../services/claude';
import { forwardAsyncErrors } from '../utils/async-routes';
import { hashPassword, hashUnusablePassword, getDummyPasswordHash } from '../utils/password-hash';
import { clearPaidTrialCookie, expirePromotedTrialCookies } from '../middleware/trial-cookie';

declare module 'express-session' {
  interface SessionData {
    /**
     * The credentials this browser typed for an address that is not verified
     * yet (see "Unverified accounts" below). Server side only: it lives in
     * the session store, never in the cookie.
     */
    pendingSignup?: { email: string; passwordHash: string; name: string };
    /** Address the "check your inbox" page shows and offers to resend to. */
    verificationEmail?: string;
  }
}

const router = forwardAsyncErrors(Router());
const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_EMAIL_LENGTH = 254;
const MIN_PASSWORD_LENGTH = 8;
// bcrypt only reads the first 72 bytes of its input. A longer password would
// be silently truncated, so two different long passwords could match the same
// hash. Refuse them at signup and reset instead.
const MAX_PASSWORD_BYTES = 72;
/** Hash a token with SHA-256 before storing in DB. Raw token goes in the email URL. */
function hashToken(token: string): string {
  return crypto.createHash('sha256').update(token).digest('hex');
}

/**
 * Read a form field as a string. `express.urlencoded({ extended: true })` and
 * JSON bodies can deliver arrays or objects for any field name; calling a
 * string method on one of those throws.
 */
function field(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function passwordProblem(password: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
  }
  if (Buffer.byteLength(password, 'utf8') > MAX_PASSWORD_BYTES) {
    return `Password is too long. Please use at most ${MAX_PASSWORD_BYTES} characters.`;
  }
  return null;
}

/**
 * Origin used in emailed links (verification, password reset).
 *
 * Prefer the configured BASE_URL. Deriving it from the request lets whoever
 * sends the request choose the host that ends up in the victim's email
 * (password reset poisoning) on any deployment whose front end forwards
 * arbitrary Host headers. When BASE_URL is not set the request host is used so
 * a local checkout works out of the box, but only if it looks like a plain
 * host[:port]; anything else falls back to localhost rather than being
 * written into an email.
 */
export function publicBaseUrl(req: Request): string {
  const configured = (process.env.BASE_URL || '').trim().replace(/\/+$/, '');
  if (/^https?:\/\/[a-z0-9.-]+(:\d{1,5})?$/i.test(configured)) {
    return configured;
  }
  const host = req.get('host') || '';
  const safeHost = /^(?:[a-z0-9.-]+|\[[0-9a-f:]+\])(?::\d{1,5})?$/i.test(host) ? host : 'localhost';
  // Under 'trust proxy', req.protocol is the client's X-Forwarded-Proto
  // verbatim, so only the two schemes a link may carry are passed through.
  const scheme = req.protocol === 'https' ? 'https' : 'http';
  return `${scheme}://${safeHost}`;
}

const SESSION_CARRY_OVER_KEYS = [
  'utmSource',
  'utmMedium',
  'utmCampaign',
  'anonSessionId',
  'anonBuildId',
  'claimedAnonBuildId',
] as const;

/**
 * Log a customer in on a brand-new session id (session fixation defense: an
 * id that existed before authentication must never become an authenticated
 * session). Funnel state that is not identity (UTM tags, the anonymous draft
 * handoff) is copied across. Resolves once the store has persisted the new
 * session, so the caller can redirect without racing the async store.
 */
function startAuthenticatedSession(
  req: Request,
  customer: { id: string; email: string; name: string }
): Promise<void> {
  const carried: Partial<Record<(typeof SESSION_CARRY_OVER_KEYS)[number], string>> = {};
  for (const key of SESSION_CARRY_OVER_KEYS) {
    const value = req.session[key];
    if (typeof value === 'string') carried[key] = value;
  }
  return new Promise((resolve, reject) => {
    req.session.regenerate((regenerateErr) => {
      if (regenerateErr) {
        reject(regenerateErr);
        return;
      }
      Object.assign(req.session, carried);
      req.session.customerId = customer.id;
      req.session.customerEmail = customer.email;
      req.session.customerName = customer.name;
      req.session.authenticatedAt = Date.now();
      req.session.save((saveErr) => (saveErr ? reject(saveErr) : resolve()));
    });
  });
}

/** Pages whose URL carries a one-time token: keep them out of caches, Referer headers and analytics. */
function markTokenPage(res: Response): void {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.locals.analyticsId = '';
}

/**
 * Start `send` once the response has gone out, so that building and logging
 * a mail is not part of the time a request takes (a known address and an
 * unknown one must answer alike). Registered before the response is written;
 * 'close' covers a client that disconnects before 'finish'. Errors are logged.
 */
function sendAfterResponse(res: Response, label: string, send: () => Promise<void>): void {
  let started = false;
  const start = (): void => {
    if (started) return;
    started = true;
    send().catch((err) => console.error(`${label}:`, (err as Error).message));
  };
  res.once('finish', start);
  res.once('close', start);
}

/**
 * Attach the anonymous /try draft held by this session to `customerId` and
 * remember it for the redirect after verification. Best effort: a failure is
 * logged and the flow goes on. Clears the session's anonymous keys either
 * way, so later /try calls start fresh under the account.
 */
async function claimSessionDraft(req: Request, customerId: string): Promise<void> {
  if (!req.session.anonSessionId) return;
  try {
    const { claimed, buildId: claimedBuildId } = await claimAnonDraftBySessionId(
      req.session.anonSessionId,
      customerId
    );
    if (claimed && claimedBuildId) {
      req.session.claimedAnonBuildId = claimedBuildId;
    }
  } catch (err) {
    console.error('anon-intake: claim on signup failed:', (err as Error).message);
  }
  delete req.session.anonSessionId;
  delete req.session.anonBuildId;
}

// ---------------------------------------------------------------------------
// Unverified accounts: whose password is it?
// ---------------------------------------------------------------------------
//
// Anyone can type any address into the signup form, so a row that is not
// verified yet proves nothing about who created it. If verification simply
// switched such a row on, a stranger could register someone else's address
// with a password of their own choosing and wait: the real owner later signs
// up or asks for a new link, clicks it, and the account opens with the
// stranger's password in place.
//
// The rule here: until the mailbox is proven, the password and name on a row
// are only a claim, and the claim that counts is the one made in the browser
// that then proves the mailbox.
//
//   - POST /signup keeps what was typed (password hash and name) in that
//     browser's server-side session as `pendingSignup`. Only when the address
//     has no account does it also create the row (with what was typed, and
//     the terms stamp). An existing row, verified or not, is not written to,
//     and no link already sent is voided: the claim lives in the session
//     until a link settles it. The response is the same in all three cases.
//   - Opening a valid link for an unverified account (GET /verify-email shows
//     a confirmation page; its POST does the work) sets the account's
//     password and name in the same statement that verifies it: to this
//     browser's own `pendingSignup` when it has one for that address (which
//     also stamps the terms, if the row has no stamp yet), and otherwise to an
//     unusable random password and a neutral name. In the second case the
//     visitor is sent to choose a password. Either way, whatever an earlier
//     claimant typed does not survive verification unless it was typed in
//     this browser.
//   - Verification mail goes to an address whose owner has proven nothing
//     yet, so it greets nobody by name (whatever name exists was typed by
//     whoever created the row), and at most one link is sent to an address
//     per hour, whoever asks (ISSUE_VERIFICATION_TOKEN_SQL). Links already
//     sent keep working, so the limit costs the owner nothing.
//   - POST /login with the correct password for an unverified account records
//     the same claim before showing the "verify first" page, so someone who
//     signed up on one device can finish on another by logging in there and
//     asking for a new link.
//   - A completed password reset also proves the mailbox, so it verifies the
//     account along with replacing the password.
//
// Cost, accepted: a person who signs up in one browser and opens the link in
// another, without logging in there first, is asked to choose a password
// again and gets a name derived from their address.

/** The stored claim if it is for this address, else null. */
function claimFor(req: Request, email: string): { passwordHash: string; name: string } | null {
  const claim = req.session.pendingSignup;
  if (!claim || claim.email !== email) return null;
  if (typeof claim.passwordHash !== 'string' || typeof claim.name !== 'string') return null;
  return { passwordHash: claim.passwordHash, name: claim.name };
}

// Each statement that issues a token also drops tokens that expired long ago,
// so the table cannot grow without bound. (A data-modifying CTE runs exactly
// once whether or not the outer statement reads from it.)

// Issue a verification link for the address $1 (token hash $2), in one
// statement that runs whether or not the address has an account: it inserts
// only for an unverified account that has been sent no link in the last hour,
// and returns a row only when it inserted. A link lives 24 hours (the
// column's default), so "issued in the last hour" is "expires more than 23
// hours from now". Two requests at the same instant can each issue one: the
// check reads the table as it was when the statement started.
const ISSUE_VERIFICATION_TOKEN_SQL = `
  WITH pruned AS (
    DELETE FROM email_verification_tokens
    WHERE expires_at < NOW() - INTERVAL '7 days'
  ),
  target AS (
    SELECT c.id FROM customers c
     WHERE c.email = $1
       AND c.email_verified IS NOT TRUE
       AND NOT EXISTS (
         SELECT 1 FROM email_verification_tokens t
          WHERE t.customer_id = c.id
            AND t.expires_at > NOW() + INTERVAL '23 hours')
  )
  INSERT INTO email_verification_tokens (customer_id, token)
  SELECT id, $2 FROM target
  RETURNING customer_id`;

// Issue a reset link for the address $1 (token hash $2), in one statement
// that runs whether or not the address has an account; it inserts, and
// returns the account's name and whether it is verified, only when there is one.
const ISSUE_PASSWORD_RESET_TOKEN_SQL = `
  WITH pruned AS (
    DELETE FROM password_reset_tokens
    WHERE expires_at < NOW() - INTERVAL '1 day'
  ),
  target AS (
    SELECT id, name, email_verified FROM customers WHERE email = $1
  ),
  issued AS (
    INSERT INTO password_reset_tokens (customer_id, token)
    SELECT id, $2 FROM target
    RETURNING customer_id
  )
  SELECT target.name, target.email_verified
    FROM issued JOIN target ON target.id = issued.customer_id`;

// A reset link for a known account (POST /verify-email hands one to a visitor
// whose browser made no claim).
const INSERT_PASSWORD_RESET_TOKEN_SQL = `
  WITH pruned AS (
    DELETE FROM password_reset_tokens
    WHERE expires_at < NOW() - INTERVAL '1 day'
  )
  INSERT INTO password_reset_tokens (customer_id, token)
  VALUES ($1, $2)`;

router.get('/signup', async (req: Request, res: Response) => {
  // Stash UTM params in session so they survive the form POST
  const utmSource = field(req.query.utm_source).trim().slice(0, 255);
  const utmMedium = field(req.query.utm_medium).trim().slice(0, 255);
  const utmCampaign = field(req.query.utm_campaign).trim().slice(0, 255);
  if (utmSource) req.session.utmSource = utmSource;
  if (utmMedium) req.session.utmMedium = utmMedium;
  if (utmCampaign) req.session.utmCampaign = utmCampaign;

  // If the visitor is coming from the /try funnel, surface their latest
  // spec preview on the signup page as proof the AI understood them.
  // Non-blocking: a DB hiccup just hides the panel.
  let specPreview: AnonSpecPreview | null = null;
  if (req.session.anonBuildId && req.session.anonSessionId) {
    try {
      const row = await pool.query<{ spec: { preview?: AnonSpecPreview } | null }>(
        `SELECT spec FROM builds
         WHERE id = $1 AND customer_id IS NULL AND anonymous_session_id = $2`,
        [req.session.anonBuildId, req.session.anonSessionId]
      );
      specPreview = row.rows[0]?.spec?.preview ?? null;
    } catch (err) {
      console.error('GET /signup spec preview lookup failed:', (err as Error).message);
    }
  }

  const csrfToken = generateCsrfToken(req);
  res.render('signup', {
    title: 'Create Account',
    csrfToken,
    error: null,
    email: '',
    name: '',
    specPreview,
  });
});

router.post('/signup', signupRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const body = (req.body || {}) as Record<string, unknown>;
  const email = field(body.email);
  const password = field(body.password);
  const name = field(body.name);

  const renderError = (error: string): void => {
    res.render('signup', {
      title: 'Create Account',
      csrfToken: generateCsrfToken(req),
      error,
      email,
      name,
    });
  };

  if (!email || !password || !name) {
    renderError('All fields are required.');
    return;
  }

  const sanitizedEmail = email.trim().toLowerCase();
  const sanitizedName = name.trim().slice(0, 255);

  if (sanitizedEmail.length > MAX_EMAIL_LENGTH || !EMAIL_REGEX.test(sanitizedEmail)) {
    renderError('Please enter a valid email address.');
    return;
  }

  const problem = passwordProblem(password);
  if (problem) {
    renderError(problem);
    return;
  }

  try {
    // Hash before looking the email up, so a taken address and a free one do
    // the same bcrypt work and cannot be told apart by response time.
    const hashedPassword = await hashPassword(password);

    // Recorded for every outcome below, before the lookup, so the response
    // (including whether a session cookie is set) does not depend on whether
    // the address is free, claimed or verified.
    req.session.pendingSignup = { email: sanitizedEmail, passwordHash: hashedPassword, name: sanitizedName };
    req.session.verificationEmail = sanitizedEmail;
    const sent = (): void => res.redirect('/verify-email-sent');

    const existing = await pool.query(
      'SELECT id, email_verified FROM customers WHERE email = $1',
      [sanitizedEmail]
    );
    const existingRow = existing.rows[0] as { id: string; email_verified: boolean | null } | undefined;

    if (existingRow && existingRow.email_verified === true) {
      // A verified account is its owner's. Nothing is changed and nothing is
      // sent; the redirect is the same one a new signup gets, so the form
      // does not reveal that the address is registered.
      sent();
      return;
    }

    // An unverified row is not written to: whoever created it has shown
    // nothing, and neither has this browser. The claim recorded above is what
    // a link opened here settles (see "Unverified accounts" above); links
    // already sent to the address keep working.
    let createdId: string | null = null;
    if (!existingRow) {
      // Capture UTM attribution from session (stashed on GET /signup)
      const utmSource = (req.session.utmSource || '').slice(0, 255) || null;
      const utmMedium = (req.session.utmMedium || '').slice(0, 255) || null;
      const utmCampaign = (req.session.utmCampaign || '').slice(0, 255) || null;

      const result = await pool.query(
        `INSERT INTO customers (email, password, name, terms_accepted_at, utm_source, utm_medium, utm_campaign)
         VALUES ($1, $2, $3, NOW(), $4, $5, $6)
         ON CONFLICT (email) DO NOTHING
         RETURNING id, email, name`,
        [sanitizedEmail, hashedPassword, sanitizedName, utmSource, utmMedium, utmCampaign]
      );
      createdId = (result.rows[0] as { id: string } | undefined)?.id ?? null;
      // No row: someone registered the address between the lookup and the
      // insert. It is then an existing account like any other.
    }

    // A link (24-hour expiry; only its SHA-256 is stored), unless the address
    // was sent one in the last hour or was verified a moment ago.
    const verifyToken = crypto.randomBytes(32).toString('hex');
    const issued = await pool.query(ISSUE_VERIFICATION_TOKEN_SQL, [sanitizedEmail, hashToken(verifyToken)]);
    if (issued.rows.length > 0) {
      const verifyUrl = `${publicBaseUrl(req)}/verify-email?token=${verifyToken}`;
      sendAfterResponse(res, 'Verification email failed', () => sendVerificationEmail(sanitizedEmail, verifyUrl));
    }

    // An anonymous /try draft goes with the account this signup created. For
    // an existing row it waits in the session until a link opened here
    // proves the mailbox (POST /verify-email).
    if (createdId) await claimSessionDraft(req, createdId);

    // Do NOT log the user in yet; require email verification first. The
    // address is not put in the redirect URL: it would end up in browser
    // history and proxy logs. The next page reads it from the session.
    sent();
  } catch (err) {
    console.error('Signup error:', (err as Error).message);
    renderError('Something went wrong. Please try again.');
  }
});

router.get('/login', (req: Request, res: Response) => {
  const csrfToken = generateCsrfToken(req);
  res.render('login', {
    title: 'Log In',
    csrfToken,
    error: null,
    email: '',
    unverifiedEmail: null,
    returnTo: safeReturnTo(req.query.returnTo),
  });
});

router.post('/login', authRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const body = (req.body || {}) as Record<string, unknown>;
  const email = field(body.email);
  const password = field(body.password);
  // The login form posts back to /login?returnTo=..., so the value arrives in
  // the query string. It is attacker-controllable either way: validate it.
  const returnTo = safeReturnTo(req.query.returnTo);

  const renderLogin = (opts: { error: string | null; email: string; unverifiedEmail?: string | null }): void => {
    res.render('login', {
      title: 'Log In',
      csrfToken: generateCsrfToken(req),
      error: opts.error,
      email: opts.email,
      unverifiedEmail: opts.unverifiedEmail ?? null,
      returnTo,
    });
  };

  if (!email || !password) {
    renderLogin({ error: 'Email and password are required.', email });
    return;
  }

  const sanitizedEmail = email.trim().toLowerCase();

  if (sanitizedEmail.length > MAX_EMAIL_LENGTH || !EMAIL_REGEX.test(sanitizedEmail)) {
    renderLogin({ error: 'Please enter a valid email address.', email });
    return;
  }

  try {
    const result = await pool.query(
      'SELECT id, email, name, password, email_verified FROM customers WHERE email = $1',
      [sanitizedEmail]
    );

    const customer = result.rows[0] as
      | { id: string; email: string; name: string; password: string; email_verified: boolean }
      | undefined;

    // Always run one bcrypt comparison, against a throwaway hash when the
    // account does not exist, so both failures take the same time.
    const passwordMatch = await bcrypt.compare(
      password,
      customer ? customer.password : await getDummyPasswordHash()
    );

    if (!customer || !passwordMatch) {
      renderLogin({ error: 'Invalid email or password.', email: sanitizedEmail });
      return;
    }

    // Block login for unverified accounts. Only reachable with the correct
    // password, so it does not reveal which emails are registered.
    if (!customer.email_verified) {
      // The password was right, so this browser holds the account's current
      // claim: remember it, so that a link opened here keeps this password
      // (see "Unverified accounts" above). Also what the resend page shows.
      req.session.pendingSignup = {
        email: customer.email,
        passwordHash: customer.password,
        name: customer.name,
      };
      req.session.verificationEmail = customer.email;
      renderLogin({ error: null, email: sanitizedEmail, unverifiedEmail: sanitizedEmail });
      return;
    }

    await startAuthenticatedSession(req, customer);
    res.redirect(returnTo || '/dashboard');
  } catch (err) {
    console.error('Login error:', (err as Error).message);
    renderLogin({ error: 'Something went wrong. Please try again.', email: sanitizedEmail });
  }
});

router.post('/logout', validateCsrfToken, async (req: Request, res: Response) => {
  // The paid trial cookie belongs to the browser, not to the account session:
  // logout clears it too (middleware/trial-cookie.ts, "After payment").
  await clearPaidTrialCookie(pool, req, res);
  req.session.destroy((err) => {
    if (err) {
      console.error('Logout error:', err.message);
    }
    res.clearCookie(SESSION_COOKIE_NAME, sessionCookieAttributes());
    res.redirect('/');
  });
});

// --- Email verification ---

// The address comes from the session (set by signup, by a login attempt on an
// unverified account, or by the resend form), never from the query string.
router.get('/verify-email-sent', (req: Request, res: Response) => {
  const email = field(req.session.verificationEmail).slice(0, MAX_EMAIL_LENGTH);
  const csrfToken = generateCsrfToken(req);
  res.render('verify-email-sent', {
    title: 'Check Your Email',
    email,
    csrfToken,
    error: null,
  });
});

// Which unverified account a verification link is for, while the link is
// still good. Read only: a cheap look before anything is spent or hashed.
const PENDING_VERIFICATION_SQL = `SELECT c.email
         FROM email_verification_tokens t
         JOIN customers c ON c.id = t.customer_id
        WHERE t.token = $1 AND t.expires_at > NOW() AND t.used_at IS NULL
          AND c.email_verified IS NOT TRUE`;

function renderMissingVerificationLink(req: Request, res: Response): void {
  res.status(400).render('verify-email-sent', {
    title: 'Verify Email',
    email: '',
    csrfToken: generateCsrfToken(req),
    error: 'Invalid or missing verification link.',
  });
}

function renderSpentVerificationLink(req: Request, res: Response): void {
  res.render('login', {
    title: 'Log In',
    csrfToken: generateCsrfToken(req),
    error: 'This verification link has expired or already been used. Please request a new one.',
    email: '',
    unverifiedEmail: null,
    returnTo: null,
  });
}

// Opening the emailed link spends nothing: it shows a page whose button posts
// the token back (POST /verify-email). Mail scanners and link previews fetch
// links, and a fetch must not verify the account in a browser that holds no
// claim (which would replace the password; see "Unverified accounts" above).
router.get('/verify-email', async (req: Request, res: Response) => {
  const token = field(req.query.token);
  markTokenPage(res);

  if (!token) {
    renderMissingVerificationLink(req, res);
    return;
  }

  try {
    const pending = await pool.query(PENDING_VERIFICATION_SQL, [hashToken(token)]);
    if (pending.rows.length === 0) {
      renderSpentVerificationLink(req, res);
      return;
    }
    res.render('verify-email', {
      title: 'Verify Email',
      csrfToken: generateCsrfToken(req),
      token,
    });
  } catch (err) {
    console.error('Email verification page error:', (err as Error).message);
    res.status(500).render('verify-email-sent', {
      title: 'Verify Email',
      email: '',
      csrfToken: generateCsrfToken(req),
      error: 'Something went wrong. Please try again.',
    });
  }
});

router.post('/verify-email', validateCsrfToken, async (req: Request, res: Response) => {
  const token = field((req.body || {}).token);
  markTokenPage(res);

  if (!token) {
    renderMissingVerificationLink(req, res);
    return;
  }

  try {
    const tokenHash = hashToken(token);

    // Cheap look first: which account is this link for, and is it still
    // waiting? A bogus or spent link stops here, before any bcrypt work.
    const pending = await pool.query(PENDING_VERIFICATION_SQL, [tokenHash]);
    const pendingEmail = (pending.rows[0] as { email: string } | undefined)?.email;
    const claim = pendingEmail ? claimFor(req, pendingEmail) : null;
    // With no claim from this browser, nothing typed by whoever created the
    // row is kept: the password becomes one nobody knows.
    const unusablePassword =
      pendingEmail && !claim
        ? await hashUnusablePassword()
        : null;

    // One statement spends the link, verifies the account and settles its
    // password and name. It is atomic: two requests racing on the same link
    // cannot both succeed, and there is no moment at which the account is
    // verified while still carrying an earlier claimant's password. It only
    // ever applies to an account that is not verified yet. This browser's own
    // claim was made on a form that accepts the terms, so it also stamps them
    // on a row that has no stamp (an account a payment or a dispatch created).
    const result = pendingEmail
      ? await pool.query(
          `WITH claimed AS (
             UPDATE email_verification_tokens
                SET used_at = NOW()
              WHERE token = $1 AND expires_at > NOW() AND used_at IS NULL
              RETURNING customer_id
           )
           UPDATE customers c
              SET email_verified = TRUE,
                  password = CASE WHEN $2::text IS NOT NULL AND c.email = $2::text THEN $3::text ELSE $5::text END,
                  name = CASE WHEN $2::text IS NOT NULL AND c.email = $2::text THEN $4::text
                              ELSE split_part(c.email, '@', 1) END,
                  terms_accepted_at = CASE WHEN $2::text IS NOT NULL AND c.email = $2::text
                                           THEN COALESCE(c.terms_accepted_at, NOW())
                                           ELSE c.terms_accepted_at END
             FROM claimed
            WHERE c.id = claimed.customer_id
              AND c.email_verified IS NOT TRUE
              AND ($2::text IS NOT NULL OR $5::text IS NOT NULL)
            RETURNING c.id AS customer_id, c.email, c.name,
                      ($2::text IS NOT NULL AND c.email = $2::text) AS own_claim`,
          [
            tokenHash,
            claim ? pendingEmail : null,
            claim ? claim.passwordHash : null,
            claim ? claim.name : null,
            unusablePassword,
          ]
        )
      : { rows: [] as unknown[] };

    if (result.rows.length === 0) {
      renderSpentVerificationLink(req, res);
      return;
    }

    const row = result.rows[0] as { customer_id: string; email: string; name: string; own_claim: boolean };

    // Any other link still outstanding for this account is now pointless
    // (the statement above refuses a verified account); retire them.
    await pool.query(
      'UPDATE email_verification_tokens SET used_at = NOW() WHERE customer_id = $1 AND used_at IS NULL',
      [row.customer_id]
    );
    // A paid cookie promoted into this account stops speaking for it: the
    // mailbox has been proven, and the account is reached through its own login.
    await expirePromotedTrialCookies(pool, row.customer_id);
    // The claim this link settled is spent. A claim this browser holds for a
    // different address is not, and stays for that address's link.
    if (row.own_claim) delete req.session.pendingSignup;
    if (req.session.verificationEmail === row.email) delete req.session.verificationEmail;

    // Send welcome email now that they're verified
    sendWelcomeEmail(row.email, row.name).catch(err =>
      console.error('Welcome email failed:', (err as Error).message)
    );

    // Schedule welcome nurture sequence (day 1, 3, 7 follow-ups)
    scheduleWelcomeSequence(row.customer_id).catch(err =>
      console.error('Welcome sequence scheduling failed:', (err as Error).message)
    );

    if (!row.own_claim) {
      // The mailbox is proven, but this browser never typed a password for
      // the account, and the one on the row was just replaced with a random
      // value. Hand over a single-use link to choose one. No session is
      // started: the account has no password its owner knows yet.
      const resetToken = crypto.randomBytes(32).toString('hex');
      await pool.query(INSERT_PASSWORD_RESET_TOKEN_SQL, [row.customer_id, hashToken(resetToken)]);
      res.redirect(`/reset-password?token=${resetToken}&verified=1`);
      return;
    }

    // If the visitor signed up from the /try funnel, their anon draft was
    // claimed in POST /signup when that signup created the account, and is
    // claimed now when the account already existed. Resume that build instead
    // of spinning up a fresh intake.
    await claimSessionDraft(req, row.customer_id);
    const resumeBuildId = req.session.claimedAnonBuildId;
    delete req.session.claimedAnonBuildId;

    // Log the customer in automatically after verification
    await startAuthenticatedSession(req, { id: row.customer_id, email: row.email, name: row.name });
    res.redirect(resumeBuildId ? `/intake/${encodeURIComponent(resumeBuildId)}` : '/intake');
  } catch (err) {
    console.error('Email verification error:', (err as Error).message);
    res.status(500).render('verify-email-sent', {
      title: 'Verify Email',
      email: '',
      csrfToken: generateCsrfToken(req),
      error: 'Something went wrong. Please try again.',
    });
  }
});

router.post('/resend-verification', signupRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const sanitizedEmail = field((req.body || {}).email).trim().toLowerCase().slice(0, MAX_EMAIL_LENGTH);

  // Always show the same page regardless of outcome (prevent enumeration).
  // The address travels in the session, not in the redirect URL.
  const done = (): void => {
    if (sanitizedEmail) req.session.verificationEmail = sanitizedEmail;
    res.redirect('/verify-email-sent');
  };

  if (!sanitizedEmail) return done();

  try {
    // The same one statement for every address: it issues a link only for an
    // unverified account that was sent none in the last hour. The mail, when
    // there is one, starts after the response.
    const verifyToken = crypto.randomBytes(32).toString('hex');
    const issued = await pool.query(ISSUE_VERIFICATION_TOKEN_SQL, [sanitizedEmail, hashToken(verifyToken)]);
    if (issued.rows.length > 0) {
      const verifyUrl = `${publicBaseUrl(req)}/verify-email?token=${verifyToken}`;
      sendAfterResponse(res, 'Resend verification email failed', () => sendVerificationEmail(sanitizedEmail, verifyUrl));
    }
  } catch (err) {
    console.error('Resend verification error:', (err as Error).message);
  }

  return done();
});

// --- Forgot password ---

router.get('/forgot-password', (req: Request, res: Response) => {
  const csrfToken = generateCsrfToken(req);
  res.render('forgot-password', {
    title: 'Forgot Password',
    csrfToken,
    message: null,
    error: null,
  });
});

router.post('/forgot-password', authRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const csrfToken = generateCsrfToken(req);
  const sanitizedEmail = field((req.body || {}).email).trim().toLowerCase().slice(0, MAX_EMAIL_LENGTH);

  // The response is identical whether or not the account exists, and it never
  // contains the reset link: the link is proof of control of the mailbox, so
  // it travels by email only.
  const neutral = (): void => {
    res.render('forgot-password', {
      title: 'Forgot Password',
      csrfToken,
      message: 'If that email exists, check your inbox for a reset link.',
      error: null,
    });
  };

  if (!sanitizedEmail) {
    return neutral();
  }

  try {
    // The same one statement for a known address and an unknown one; the
    // mail, when there is one, starts after the response.
    const token = crypto.randomBytes(32).toString('hex');
    const issued = await pool.query(ISSUE_PASSWORD_RESET_TOKEN_SQL, [sanitizedEmail, hashToken(token)]);
    const account = issued.rows[0] as { name: string; email_verified: boolean | null } | undefined;
    if (account) {
      const resetUrl = `${publicBaseUrl(req)}/reset-password?token=${token}`;
      // The name on an unverified row was typed by whoever created it, who
      // may not own the address: greet by name only once the mailbox's owner
      // has settled it (verification or a reset).
      const greetName = account.email_verified === true ? account.name : null;
      sendAfterResponse(res, '[auth] Password reset email failed', () =>
        sendPasswordResetEmail(sanitizedEmail, greetName, resetUrl)
      );
    }

    return neutral();
  } catch (err) {
    console.error('Forgot password error:', (err as Error).message);
    res.render('forgot-password', {
      title: 'Forgot Password',
      csrfToken,
      message: null,
      error: 'Something went wrong. Please try again.',
    });
  }
});

// --- Reset password ---

router.get('/reset-password', async (req: Request, res: Response) => {
  const token = field(req.query.token);
  const csrfToken = generateCsrfToken(req);
  markTokenPage(res);

  if (!token) {
    res.status(400).render('reset-password', {
      title: 'Reset Password',
      csrfToken,
      token: '',
      error: 'Invalid or missing reset token.',
      success: false,
    });
    return;
  }

  try {
    const result = await pool.query(
      `SELECT id FROM password_reset_tokens
       WHERE token = $1 AND expires_at > NOW() AND used_at IS NULL`,
      [hashToken(token)]
    );

    if (result.rows.length === 0) {
      res.render('reset-password', {
        title: 'Reset Password',
        csrfToken,
        token: '',
        error: 'This reset link is invalid or has expired. Please request a new one.',
        success: false,
      });
      return;
    }

    res.render('reset-password', {
      title: 'Reset Password',
      csrfToken,
      token,
      error: null,
      success: false,
      // Set by GET /verify-email when it sends a newly verified visitor here
      // to choose a password.
      justVerified: field(req.query.verified) === '1',
    });
  } catch (err) {
    console.error('Reset password GET error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Something went wrong.' });
  }
});

router.post('/reset-password', authRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const body = (req.body || {}) as Record<string, unknown>;
  const token = field(body.token);
  const password = field(body.password);
  const csrfToken = generateCsrfToken(req);
  markTokenPage(res);

  const renderForm = (error: string, keepToken: boolean): void => {
    res.render('reset-password', {
      title: 'Reset Password',
      csrfToken,
      token: keepToken ? token : '',
      error,
      success: false,
    });
  };
  const INVALID_LINK = 'This reset link is invalid or has expired. Please request a new one.';

  const problem = !token ? 'Invalid or missing reset token.' : passwordProblem(password);
  if (problem) {
    renderForm(problem, true);
    return;
  }

  try {
    // Cheap validity check first, so a bogus token cannot make the server do
    // bcrypt work.
    const pending = await pool.query(
      `SELECT id FROM password_reset_tokens
       WHERE token = $1 AND expires_at > NOW() AND used_at IS NULL`,
      [hashToken(token)]
    );

    if (pending.rows.length === 0) {
      renderForm(INVALID_LINK, false);
      return;
    }

    const hashedPassword = await hashPassword(password);

    // Spend the token and set the password in one atomic statement: the token
    // can be used exactly once even if two requests race, and it is never
    // left spent with the old password still in place. The reset link went
    // to the account's mailbox, so completing it proves control of that
    // mailbox exactly as a verification link does: the account is marked
    // verified in the same statement. (Without that, an account created for
    // a Harbor payer, which starts unverified with a random password, could
    // set a password here and still be refused at login.) The name on a row
    // that was not verified was typed by whoever created it, not necessarily
    // the mailbox's owner, so it is replaced by the address's local part, as
    // a verification link opened without this browser's claim does.
    const updated = await pool.query(
      `WITH claimed AS (
         UPDATE password_reset_tokens
            SET used_at = NOW()
          WHERE token = $1 AND expires_at > NOW() AND used_at IS NULL
          RETURNING customer_id
       )
       UPDATE customers c
          SET password = $2, email_verified = TRUE,
              name = CASE WHEN c.email_verified IS TRUE THEN c.name
                          ELSE split_part(c.email, '@', 1) END
         FROM claimed
        WHERE c.id = claimed.customer_id
        RETURNING c.id AS customer_id`,
      [hashToken(token), hashedPassword]
    );

    if (updated.rows.length === 0) {
      renderForm(INVALID_LINK, false);
      return;
    }

    const customerId = (updated.rows[0] as { customer_id: string }).customer_id;

    // Any other reset link still outstanding for this account dies with the
    // old password, and so does any verification link: the account is
    // verified now.
    await pool.query(
      'UPDATE password_reset_tokens SET used_at = NOW() WHERE customer_id = $1 AND used_at IS NULL',
      [customerId]
    );
    await pool.query(
      'UPDATE email_verification_tokens SET used_at = NOW() WHERE customer_id = $1 AND used_at IS NULL',
      [customerId]
    );

    // Invalidate all active sessions for this user (security: compromised sessions must not survive password reset)
    await pool.query(
      `DELETE FROM user_sessions WHERE sess::jsonb->>'customerId' = $1`,
      [customerId]
    );
    // The same holds for a paid trial cookie promoted into this account: a
    // browser that paid before the password changed no longer speaks for it.
    await expirePromotedTrialCookies(pool, customerId);

    res.render('reset-password', {
      title: 'Reset Password',
      csrfToken,
      token: '',
      error: null,
      success: true,
    });
  } catch (err) {
    console.error('Reset password POST error:', (err as Error).message);
    renderForm('Something went wrong. Please try again.', true);
  }
});

// --- Unsubscribe ---

const UNSUBSCRIBE_LINK_INVALID = 'This unsubscribe link is invalid or you are already unsubscribed.';

/**
 * The address an unsubscribe link speaks for, or null. The link carries the
 * address and HMAC-SHA256(SESSION_SECRET, address); the HMAC is recomputed and
 * compared in constant time. Reads nothing from the database.
 */
function unsubscribeAddress(emailField: unknown, tokenField: unknown): string | null {
  const email = field(emailField).trim().toLowerCase();
  const token = field(tokenField);
  if (!email || !token) return null;
  return timingSafeEqualStrings(generateUnsubscribeToken(email), token) ? email : null;
}

// Opening the link changes nothing: it shows a page whose button posts the
// link's values back (POST /unsubscribe), so a mail scanner or a link preview
// fetching the URL does not unsubscribe anyone.
router.get('/unsubscribe', (req: Request, res: Response) => {
  if (!field(req.query.token)) {
    res.status(400).render('unsubscribe', {
      title: 'Unsubscribe',
      error: 'Invalid unsubscribe link.',
      success: false,
    });
    return;
  }
  markTokenPage(res);
  const email = unsubscribeAddress(req.query.email, req.query.token);
  if (!email) {
    res.render('unsubscribe', { title: 'Unsubscribe', error: UNSUBSCRIBE_LINK_INVALID, success: false });
    return;
  }
  res.render('unsubscribe', {
    title: 'Unsubscribe',
    error: null,
    success: false,
    confirmation: { email, token: field(req.query.token) },
    csrfToken: generateCsrfToken(req),
  });
});

router.post('/unsubscribe', validateCsrfToken, async (req: Request, res: Response) => {
  const body = (req.body || {}) as Record<string, unknown>;
  markTokenPage(res);

  try {
    let matchedId: string | null = null;

    const sanitizedEmail = unsubscribeAddress(body.email, body.token);
    if (sanitizedEmail) {
      const result = await pool.query(
        'SELECT id FROM customers WHERE email = $1 AND email_unsubscribed = FALSE',
        [sanitizedEmail]
      );
      if (result.rows.length > 0) {
        matchedId = (result.rows[0] as { id: string }).id;
      }
    }

    if (!matchedId) {
      res.render('unsubscribe', {
        title: 'Unsubscribe',
        error: UNSUBSCRIBE_LINK_INVALID,
        success: false,
      });
      return;
    }

    await pool.query(
      'UPDATE customers SET email_unsubscribed = TRUE WHERE id = $1',
      [matchedId]
    );

    res.render('unsubscribe', {
      title: 'Unsubscribe',
      error: null,
      success: true,
    });
  } catch (err) {
    console.error('Unsubscribe error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Something went wrong.' });
  }
});

// --- Account deletion (GDPR right to erasure) ---

/**
 * What POST /account/delete does, table by table, for whoever has to say it
 * to a customer: the privacy page's "Data Retention" section states exactly
 * these two lists (views.marketing-accuracy.test.ts holds the page to them,
 * and money-path.realdb.test.ts holds them to what the statements below
 * really leave in a database).
 *
 * REMOVES: rows that carry the account, its conversations or its messages
 * are gone when the transaction commits (deleted by a statement here, or by
 * a cascade from the customers row or a trial row).
 *
 * LEAVES: rows that stay. `builds` keeps every build with `customer_id` set
 * to NULL, and with it the spec (which can hold the account's email address
 * as `admin_email`, and what the customer said about their work), the stored
 * artifact and the deployment's identifiers; `build_logs` hangs off those
 * rows. `email_log` and `newsletter_subscribers` are keyed by address, not by
 * account, and no statement here touches them. `page_views` keeps its rows
 * with `customer_id` set to NULL by the foreign key.
 *
 * Nothing here contacts Railway: the deployed website and its own database
 * are not touched by deleting the account.
 */
export const ACCOUNT_DELETION_REMOVES = [
  'customers',
  'keepers',
  'trial_keepers',
  'companion_turns',
  'keeper_memory',
  'intake_messages',
  'feature_requests',
  'feature_messages',
  'beta_feedback',
  'email_schedule',
  'harbor_subscriptions',
  'build_start_events',
  'password_reset_tokens',
  'email_verification_tokens',
  'user_sessions',
] as const;
export const ACCOUNT_DELETION_LEAVES = ['builds', 'build_logs', 'email_log', 'newsletter_subscribers', 'page_views'] as const;

/**
 * Which Haven trials belong to an account, as a condition over
 * `trial_keepers t` and the account's own row `customers c` ($1 is the
 * customer id). A keeper's turns and notes cascade from the customer row, but
 * a trial row references no customer: it is tied to one by the build stamped
 * on it (the link the paying cookie is recognized by) and by the address
 * captured on it. So a trial is the account's when it is stamped with one of
 * the account's builds, or when it carries the account's address and no other
 * account's build is stamped on it.
 */
const ACCOUNT_TRIALS_SQL = `c.id = $1
  AND (t.handoff_build_id IN (SELECT id FROM builds WHERE customer_id = $1)
       OR (LOWER(BTRIM(t.email_captured)) = LOWER(c.email)
           AND NOT EXISTS (
             SELECT 1 FROM builds b
              WHERE b.id = t.handoff_build_id
                AND b.customer_id IS NOT NULL
                AND b.customer_id <> $1)))`;

router.get('/account/delete', requireAuth, (req: Request, res: Response) => {
  const csrfToken = generateCsrfToken(req);
  res.render('account-delete', {
    title: 'Delete Account',
    csrfToken,
    error: null,
  });
});

router.post('/account/delete', requireAuth, accountDeleteRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const password = field((req.body || {}).password);
  const customerId = req.session.customerId as string;
  const csrfToken = generateCsrfToken(req);

  const renderError = (error: string): void => {
    res.render('account-delete', { title: 'Delete Account', csrfToken, error });
  };

  if (!password) {
    renderError('Please enter your password to confirm.');
    return;
  }

  try {
    const result = await pool.query(
      'SELECT password FROM customers WHERE id = $1',
      [customerId]
    );

    if (result.rows.length === 0) {
      renderError('Account not found.');
      return;
    }

    const passwordMatch = await bcrypt.compare(password, (result.rows[0] as { password: string }).password);
    if (!passwordMatch) {
      renderError('Incorrect password.');
      return;
    }

    // Deleting the customer cascades to harbor_subscriptions, which is the
    // only local record of a running Stripe subscription. Stop the billing
    // first, before any transaction is open: a call to Stripe can take
    // seconds, and holding a database transaction (and a pooled connection)
    // across it would be holding locks on the network's time. If Stripe
    // cannot be reached this throws and nothing has been deleted. Asking
    // Stripe to cancel at period end twice is harmless, so a deletion that
    // fails later can simply be tried again. Ids minted by the local payment
    // bypass ("sub_bypass_...") never existed at Stripe and are skipped.
    const openSubscriptions = await pool.query<{ stripe_subscription_id: string }>(
      `SELECT stripe_subscription_id FROM harbor_subscriptions
       WHERE keeper_customer_id = $1 AND status <> 'canceled'`,
      [customerId]
    );
    const billingStopped = new Set<string>();
    for (const sub of openSubscriptions.rows) {
      const subscriptionId = sub.stripe_subscription_id;
      if (!subscriptionId) continue;
      if (!subscriptionId.startsWith('sub_bypass_')) {
        await cancelHarborSubscriptionAtPeriodEnd(subscriptionId);
      }
      billingStopped.add(subscriptionId);
    }

    // Delete all personal data in a transaction for atomicity
    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      // Lock the account row, then look at its subscriptions again. The lock
      // keeps a payment that is being recorded right now from adding a
      // subscription underneath the delete (the webhook's insert references
      // this row and waits). One that was recorded after the Stripe calls
      // above has not been cancelled, so the deletion stops and can be
      // retried rather than orphan a subscription that is still billing.
      // The account's builds are locked first because that is the order the
      // payment webhook takes its locks in (the build, then rows that
      // reference the customer); the other order could deadlock with it.
      // The account's Haven trials, deleted further down, are locked between
      // the two for the same reason: the webhook locks a trial after its
      // build, and a dispatch locks its trial before its new build row
      // references the customer.
      await client.query('SELECT id FROM builds WHERE customer_id = $1 FOR UPDATE', [customerId]);
      await client.query(
        `SELECT t.trial_id FROM trial_keepers t JOIN customers c ON ${ACCOUNT_TRIALS_SQL} FOR UPDATE OF t`,
        [customerId]
      );
      await client.query('SELECT id FROM customers WHERE id = $1 FOR UPDATE', [customerId]);
      const stillOpen = await client.query<{ stripe_subscription_id: string }>(
        `SELECT stripe_subscription_id FROM harbor_subscriptions
         WHERE keeper_customer_id = $1 AND status <> 'canceled'`,
        [customerId]
      );
      const uncancelled = stillOpen.rows.filter((sub) => !billingStopped.has(sub.stripe_subscription_id));
      if (uncancelled.length > 0) {
        throw new Error('a subscription was recorded while the account was being deleted');
      }

      // Stripe goes on sending events for these subscriptions (the
      // cancellation itself, then the end of the period). Their rows are about
      // to cascade away; the tombstones let the webhook acknowledge those
      // events instead of refusing them as events that overtook a checkout.
      await client.query(
        `INSERT INTO harbor_subscription_tombstones (stripe_subscription_id)
         SELECT stripe_subscription_id FROM harbor_subscriptions WHERE keeper_customer_id = $1
         ON CONFLICT (stripe_subscription_id) DO NOTHING`,
        [customerId]
      );

      // A build still waiting in the queue would never be built: the worker
      // claims a queued build through a join to its customer, and the row is
      // about to lose that link. Close it out here. A build the worker has
      // already claimed (building, testing, deploying) is not touched: the
      // worker read what it needs at the claim and finishes or fails that
      // build itself.
      await client.query(
        `UPDATE builds
            SET status = 'failed',
                error_message = 'The account was deleted before this build started.',
                completed_at = NOW()
          WHERE customer_id = $1 AND status = 'queued'`,
        [customerId]
      );

      await client.query(
        `DELETE FROM intake_messages WHERE build_id IN (SELECT id FROM builds WHERE customer_id = $1)`,
        [customerId]
      );
      await client.query(
        `DELETE FROM feature_messages WHERE feature_request_id IN (
           SELECT id FROM feature_requests WHERE customer_id = $1
         )`,
        [customerId]
      );
      await client.query('DELETE FROM feature_requests WHERE customer_id = $1', [customerId]);
      await client.query('DELETE FROM beta_feedback WHERE customer_id = $1', [customerId]);
      await client.query('DELETE FROM email_schedule WHERE customer_id = $1', [customerId]);
      // The account's Haven trials go with it (see ACCOUNT_TRIALS_SQL); their
      // turns and notes cascade from the trial row. This has to run while the
      // builds still name their customer, so before the statement that
      // unlinks them.
      await client.query(`DELETE FROM trial_keepers t USING customers c WHERE ${ACCOUNT_TRIALS_SQL}`, [customerId]);
      // What is left carrying the address is a trial stamped with another
      // account's build: that conversation is theirs and stays, without the
      // address.
      await client.query(
        `UPDATE trial_keepers t SET email_captured = NULL
           FROM customers c
          WHERE c.id = $1 AND LOWER(BTRIM(t.email_captured)) = LOWER(c.email)`,
        [customerId]
      );
      // Anonymize builds: keep for operational records, strip customer link
      await client.query('UPDATE builds SET customer_id = NULL WHERE customer_id = $1', [customerId]);
      // Delete customer (password_reset_tokens cascade via ON DELETE CASCADE)
      await client.query('DELETE FROM customers WHERE id = $1', [customerId]);
      // Sessions on other devices must not outlive the account.
      await client.query(
        `DELETE FROM user_sessions WHERE sess::jsonb->>'customerId' = $1`,
        [customerId]
      );
      await client.query('COMMIT');
    } catch (txErr) {
      // Report the original failure, not a failure of the rollback after it.
      try {
        await client.query('ROLLBACK');
      } catch (rollbackErr) {
        console.error('Account deletion rollback failed:', (rollbackErr as Error).message);
      }
      throw txErr;
    } finally {
      client.release();
    }

    req.session.destroy((err) => {
      if (err) console.error('Session destroy error after account deletion:', err.message);
      res.clearCookie(SESSION_COOKIE_NAME, sessionCookieAttributes());
      res.redirect('/?deleted=1');
    });
  } catch (err) {
    console.error('Account deletion error:', (err as Error).message);
    renderError('Something went wrong. Please try again.');
  }
});

export default router;
