import express, { Request, Response, NextFunction } from 'express';
import * as Sentry from '@sentry/node';
import session from 'express-session';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import dotenv from 'dotenv';
const expressLayouts = require('express-ejs-layouts');
const pgSession = require('connect-pg-simple')(session);
import authRouter from './routes/auth';
import intakeRouter from './routes/intake';
import anonIntakeRouter from './routes/anon-intake';
import buildsRouter from './routes/builds';
import webhooksRouter from './routes/webhooks';
import adminRouter from './routes/admin';
import supportRouter from './routes/support';
import featuresRouter from './routes/features';
import galleryRouter from './routes/gallery';
import opsApiRouter from './routes/ops-api';
import feedbackRouter from './routes/feedback';
import { createLighthouseRouter, crisisFirst } from './routes/lighthouse';
// Tier pricing constants + beta-mode helpers were removed 2026-05-01.
// Harbor sells one subscription product; see harborSubscriptionPrice/-SlidingScalePrice below.
import {
  generateCsrfToken,
  validateCsrfToken,
  initSessionSecret,
  isAdminSession,
  timingSafeEqualStrings,
  rejectCrossOriginWrites,
  enforceSessionLifetime,
  isDevOrTestEnv,
  SESSION_ABSOLUTE_MAX_MS,
  SESSION_COOKIE_NAME,
  SESSION_IDLE_TIMEOUT_MS,
  sessionCookieAttributes,
} from './middleware/auth';
import { boundedRateLimit, rateLimitIpKey } from './middleware/companion-rate-limit';
import { createMintLimiter, TRIAL_COOKIE_NAME, TRIAL_TTL_DAYS } from './middleware/trial-cookie';
import { PAID_TRIAL_ACCESS_DAYS } from './services/keeper_promotion';
import {
  MAX_ACTIVE_BUILDS_PER_CUSTOMER,
  MAX_BUILD_STARTS_PER_DAY,
  MAX_MANUAL_RETRIES_PER_BUILD,
} from './services/build-start-limits';
import { forwardAsyncErrors } from './utils/async-routes';
import { loggablePath, recordRecentError } from './utils/recent-errors';
import pool from './db';
import { httpUrl } from './utils/format';
import {
  organizationJsonLd,
  oceanPersonJsonLd,
  faqPageJsonLd,
  jsonLdGraph,
  jsonLdScriptTag,
  escapeJsonForScript,
} from './services/structured-data';
import { pricingFaqs } from './services/pricing-faq';
import {
  listComparisonPages,
  comparisonPageJsonLd,
  comparisonHubJsonLd,
} from './data/comparison-pages';
import { resolveOgImage } from './services/og-images';
import { getLastmodForUrl } from './services/sitemap-lastmod';
import { renderErrorPage } from './utils/error-page';
import { isJsonRequest, sendJsonError } from './utils/json-request';

dotenv.config();

const app = express();
// Express 4 ignores the promise an async handler returns, so a rejection would
// hang the request and, as an unhandled rejection, end the process. Every
// router is created through forwardAsyncErrors (utils/async-routes.ts); this
// does the same for the handlers registered on the app itself further down
// (app.get, app.post, ...). The helper only touches those verb methods, which
// the application object shares with a Router, hence the cast.
forwardAsyncErrors(app as unknown as express.Router);
app.disable('x-powered-by');
// Error text is shown to the client only where the reader is the developer.
const showErrorDetail = isDevOrTestEnv();
// Secure cookies and HSTS are on unless this is explicitly a development or
// test run. A deployment that forgot NODE_ENV, or runs as "staging", gets the
// hardened transport settings rather than the local-development ones (the
// same rule the session secret and the development bypasses follow).
const hardenTransport = !isDevOrTestEnv();

// Resolve the session secret before anything can sign with it. Outside
// development and test a missing, placeholder, or short secret is fatal; see
// resolveSessionSecret in middleware/auth.ts for the full rule.
let sessionSecret: string;
try {
  const resolution = initSessionSecret();
  sessionSecret = resolution.secret;
  if (resolution.warning) console.warn(resolution.warning);
} catch (err) {
  console.error(`FATAL: ${(err as Error).message}`);
  process.exit(1);
}

if (!isDevOrTestEnv() && !(process.env.BASE_URL || '').trim()) {
  console.warn(
    'WARNING: BASE_URL is not set. Links in verification and password reset emails are built ' +
      'from the request Host header, which the sender of the request controls unless the proxy ' +
      'in front of this app pins it, and the site and unsubscribe links in the other emails ' +
      '(welcome, reminders, build notices) point at https://craftandship.io. Set BASE_URL to ' +
      'the public origin.'
  );
}

// App version from version.json (with Railway env var fallback)
const appVersion = (() => {
  try {
    const vPath = path.resolve(__dirname, '..', '..', 'version.json');
    return JSON.parse(fs.readFileSync(vPath, 'utf-8')).version;
  } catch { return process.env.APP_VERSION || 'unknown'; }
})();

/**
 * How many reverse proxies sit in front of this process (TRUST_PROXY_HOPS).
 * With n proxies, req.ip is the address the outermost of them saw and
 * req.secure follows its X-Forwarded-Proto, which Secure cookies need. The
 * number must match the deployment. Too high (1 with the app reached
 * directly) and any client can forge X-Forwarded-For and walk past every
 * per-address rate limit; too low and all visitors share the proxy's address
 * and the Secure session cookie is never issued.
 *
 * Unset means 1. An unset variable gets the production behavior everywhere in
 * this app (see isDevOrTestEnv), and the production this app has is one
 * TLS-terminating proxy: the other production defaults (Secure cookies, HSTS)
 * do not work without it, and with 0 behind that proxy nobody could log in.
 * The case where 1 is wrong is a server with nothing in front, which is what
 * a local run is, so web/.env.example sets 0 explicitly, and an unset or
 * unreadable value is called out at boot (not under NODE_ENV=test, where the
 * suite imports the app hundreds of times).
 */
export function resolveTrustProxyHops(
  env: Record<string, string | undefined> = process.env
): { hops: number; warning: string | null } {
  const raw = (env.TRUST_PROXY_HOPS ?? '').trim();
  if (/^\d{1,3}$/.test(raw)) return { hops: Number(raw), warning: null };
  const problem = raw === '' ? 'is not set' : `is "${raw.slice(0, 20)}", which is not a whole number`;
  return {
    hops: 1,
    warning:
      env.NODE_ENV === 'test'
        ? null
        : `WARNING: TRUST_PROXY_HOPS ${problem}. Assuming 1: one reverse proxy in front of this ` +
          'server, whose X-Forwarded-For and X-Forwarded-Proto headers are believed. That is right ' +
          'behind a single proxy such as Railway. If clients reach this server directly, set ' +
          'TRUST_PROXY_HOPS=0 (web/.env.example does), or any client can forge its address and ' +
          'walk past every per-address rate limit.',
  };
}

const trustProxy = resolveTrustProxyHops();
if (trustProxy.warning) console.warn(trustProxy.warning);
app.set('trust proxy', trustProxy.hops);

const publicDir = path.join(__dirname, '../public');
const STATIC_ASSET_EXTENSIONS = /\.(css|js|png|jpg|jpeg|svg|ico|webp|woff2?|ttf|eot|map)$/i;

/**
 * True when the request path names a file that exists under public/ and has a
 * static-asset extension, i.e. what express.static is about to serve. Matching
 * the extension alone would exempt any route whose path happens to end in
 * ".png" (for example /lighthouse/lamp/x.png, which reaches the trial-cookie
 * middleware, or /builds/x.css) from the password.
 */
function isPublicStaticAsset(requestPath: string): boolean {
  if (!STATIC_ASSET_EXTENSIONS.test(requestPath)) return false;
  try {
    const file = path.resolve(publicDir, '.' + decodeURIComponent(requestPath));
    if (!file.startsWith(publicDir + path.sep)) return false;
    return fs.statSync(file).isFile();
  } catch {
    // Undecodable escapes, a NUL byte, a missing file: not an asset.
    return false;
  }
}

// Security headers, including a per-request CSP nonce
// (harden-csp-remove-unsafe-inline, 2026-05-09).
//
// This is the first middleware in the stack, ahead of the preview password
// gate, the body parsers, the session and express.static, so every response
// carries these headers: pages, files from public/, the password challenge and
// the error pages that body parsing produces. It reads nothing from the
// request and sets no cookie, so where it sits changes which requests are
// answered by the gate or the cross-origin write guard in no way.
//
// Removed `'unsafe-inline'` from script-src; replaced with a per-request
// `'nonce-<base64>'` source. Every inline `<script>` tag in views must
// carry `nonce="<%= cspNonce %>"` (which is stamped into res.locals
// here) or the browser blocks execution. External scripts loaded via
// `<script src="/js/foo.js">` are still allowed by `'self'`.
//
// `unsafe-inline` is INTENTIONALLY KEPT on style-src for now: inline
// `style="..."` attributes are pervasive in the codebase, and CSP nonces
// do not apply to attributes. Tightening style-src is tracked as a
// follow-up (`harden-csp-style-src-attribute-hashes`); the load-bearing
// XSS-amplification surface was script-src, which is now closed.
app.use((_req: Request, res: Response, next: NextFunction) => {
  // 24 random bytes -> 32-char base64. Re-generated per request so
  // even if a nonce leaks via XSS in one response, it doesn't help
  // the attacker on the next.
  const nonce = crypto.randomBytes(24).toString('base64');
  res.locals.cspNonce = nonce;

  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'DENY');
  // The legacy XSS auditor is gone from current browsers and was itself an
  // attack surface in old ones; 0 switches it off where it still exists.
  res.setHeader('X-XSS-Protection', '0');
  res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
  res.setHeader('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()');
  res.setHeader(
    'Content-Security-Policy',
    `default-src 'self'; ` +
      `script-src 'self' 'nonce-${nonce}' https://plausible.io; ` +
      `style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; ` +
      `img-src 'self' data:; ` +
      `font-src 'self' https://fonts.gstatic.com; ` +
      `connect-src 'self' https://plausible.io; ` +
      `frame-src 'self'; ` +
      // None of the next four fall back to default-src, so each is explicit:
      // no <base> retargeting of relative URLs, no plugins, no framing by any
      // site (the modern form of X-Frame-Options), and forms may only post
      // here or follow our redirect to Stripe Checkout.
      `base-uri 'self'; ` +
      `object-src 'none'; ` +
      `frame-ancestors 'none'; ` +
      `form-action 'self' https://checkout.stripe.com`
  );
  if (hardenTransport) {
    res.setHeader('Strict-Transport-Security', 'max-age=31536000; includeSubDomains; preload');
  }
  next();
});

// Private beta: HTTP basic auth gate (remove PREVIEW_PASSWORD env var to open to public)
if (process.env.PREVIEW_PASSWORD) {
  const publicPaths = ['/health', '/terms', '/privacy', '/pricing', '/gallery', '/how-it-works', '/contact', '/guide', '/guide/read', '/docs/research'];
  app.use((req: Request, res: Response, next: NextFunction) => {
    if (req.path === '/' || publicPaths.includes(req.path)) return next();
    if (req.path === '/try' || req.path.startsWith('/try/')) return next(); // anon intake preview
    if (req.path.startsWith('/ops/api/')) return next(); // ops API has its own bearer auth
    if (req.path.startsWith('/webhooks/')) return next(); // Stripe webhooks need access
    if (isPublicStaticAsset(req.path)) return next(); // files in public/, served by express.static below
    const auth = req.headers.authorization;
    if (auth && auth.startsWith('Basic ')) {
      // "user:password"; the password is everything after the FIRST colon, so
      // a password that itself contains a colon still matches. Any user name
      // is accepted.
      const decoded = Buffer.from(auth.slice(6), 'base64').toString();
      const colon = decoded.indexOf(':');
      const pass = colon === -1 ? '' : decoded.slice(colon + 1);
      const expected = process.env.PREVIEW_PASSWORD || '';
      if (pass && expected && timingSafeEqualStrings(pass, expected)) return next();
    }
    res.setHeader('WWW-Authenticate', 'Basic realm="Craft & Ship Preview"');
    res.status(401).send('Access restricted.');
  });
}

// Stripe webhook needs raw body before JSON parsing
app.use('/webhooks/stripe', express.raw({ type: 'application/json' }));

// The account forms (routes/auth.ts) carry an email, a password, a name and a
// token: a few hundred bytes. They are also the routes anyone may post to
// without a session, and their rate limiter keys on what the body says, so
// they get a body limit of their own, far below the general one. A body the
// parser here has read is not read again by the general parsers below; a
// larger one is answered 413 before any route or limiter sees it. The Stripe
// raw-body parser above and the multipart logo upload (multer, in
// routes/intake.ts) are on other paths and are not touched by this.
export const AUTH_FORM_PATHS = [
  '/login',
  '/signup',
  '/logout',
  '/forgot-password',
  '/reset-password',
  '/resend-verification',
  '/verify-email',
  '/unsubscribe',
  '/account/delete',
];
export const AUTH_BODY_LIMIT = '10kb';
app.use(
  AUTH_FORM_PATHS,
  express.json({ limit: AUTH_BODY_LIMIT }),
  express.urlencoded({ extended: true, limit: AUTH_BODY_LIMIT })
);

// Body parsing with size limits to mitigate request flooding. The Resend
// webhook signature covers the exact bytes that were sent, so those are kept
// for that one route (re-serializing the parsed JSON is not guaranteed to
// reproduce them).
app.use(express.json({
  limit: '1mb',
  verify: (req, _res, buf) => {
    if ((req as Request).originalUrl.split('?')[0] === '/webhooks/resend') {
      (req as Request & { rawBody?: Buffer }).rawBody = buf;
    }
  },
}));
app.use(express.urlencoded({ extended: true, limit: '1mb' }));

// A crisis message on the Haven turn endpoint is answered before anything
// that can fail or refuse, the session store included (see crisisFirst in
// routes/lighthouse.ts; it makes its own cross-origin check).
if (process.env.CAS_HARBOR_ENABLED === 'true') {
  app.post('/lighthouse/table/turn', crisisFirst(pool));
}

// Session configuration with PostgreSQL store for persistence across deploys.
// name: 'cas.sid' prevents cookie collisions on localhost when a generated app
// (which uses the default 'connect.sid') runs side-by-side on a different port;
// browser cookies don't scope by port, so the last-written cookie would clobber
// the admin session and force re-login on every tab switch. Outside
// development and test the name is '__Host-cas.sid' (hostOnlyCookieName in
// middleware/auth.ts), which a browser accepts only from this host over HTTPS.
// A session is stored once something is written to it; rendering a page does
// that (keepSessionForRenderedPage), so the CSRF token in the page has a
// session to be checked against.
const sessionConfig: session.SessionOptions = {
  name: SESSION_COOKIE_NAME,
  secret: sessionSecret,
  resave: false,
  saveUninitialized: false,
  cookie: {
    ...sessionCookieAttributes(),
    maxAge: SESSION_IDLE_TIMEOUT_MS, // 24 hours rolling idle timeout
  },
  rolling: true, // Reset expiry on every request (idle timeout; the absolute cap is enforceSessionLifetime)
};

// Use PostgreSQL session store in production (survives deploys);
// fall back to MemoryStore in test to avoid DB dependency in unit tests
if (process.env.NODE_ENV !== 'test') {
  sessionConfig.store = new pgSession({
    pool,
    tableName: 'user_sessions',
    createTableIfMissing: false, // table created by schema.sql migration
    pruneSessionInterval: 60 * 15, // prune expired sessions every 15 min
  });
  console.log('Session store: PostgreSQL (table: user_sessions, createTableIfMissing: false)');
}

app.use(session(sessionConfig));
app.use(enforceSessionLifetime);

// View engine
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, '../views'));
app.use(expressLayouts);
app.set('layout', 'layout');

// Static files. The security headers are installed above, so these responses
// carry them. express.static answers only GET and HEAD and passes every other
// method on, so the cross-origin write guard below still sees all writes.
app.use(express.static(publicDir, { maxAge: '1d' }));

// Explicit CORS policy: same-origin only.
// (web-explicit-cors-same-origin-allowlist, 2026-05-09)
//
// CAS has no cross-origin browser callers. By never emitting an
// Access-Control-Allow-Origin header, browsers refuse cross-origin
// credentialed fetches by default (which is what we already relied on).
//
// This middleware codifies the policy explicitly so a future contributor
// adding `cors({ origin: '*' })` for one endpoint cannot silently widen
// it globally: any downstream Access-Control-Allow-Origin header set on
// the response is stripped just before headers flush.
//
// OPTIONS preflight requests carrying an Origin header are short-circuited
// with 204 + no CORS headers, blocking cross-origin scripts from
// proceeding past the preflight.
//
// Webhook endpoints (/webhooks/*) are unaffected: Stripe and other
// providers send server-to-server POSTs without an Origin header, so
// CORS is irrelevant to them.
app.use((req: Request, res: Response, next: NextFunction) => {
  const origin = req.headers.origin;
  if (req.method === 'OPTIONS' && origin) {
    // Cross-origin preflight: refuse with no CORS headers.
    res.status(204);
    res.setHeader('Content-Length', '0');
    return res.end();
  }
  // Strip any Access-Control-Allow-Origin written by downstream code
  // just before headers are sent. This is the forward-looking guard
  // against a stray cors() middleware being added for one endpoint.
  const _writeHead = res.writeHead.bind(res);
  (res as any).writeHead = (...args: unknown[]) => {
    res.removeHeader('Access-Control-Allow-Origin');
    return (_writeHead as any)(...args);
  };
  next();
});

// CSRF, layer 1: refuse state-changing requests that the browser marks as
// cross-origin (see middleware/auth.ts). Runs before every router, so it also
// covers the JSON endpoints and the few forms that carry no token.
app.use(rejectCrossOriginWrites);

// Safe JSON helper for values inlined into a <script> block: escapes "<" and
// ">" so no value can close the script element or open a comment, plus the
// two line separators that older JavaScript engines reject inside a string
// literal (same escaper as the JSON-LD blocks). undefined (which
// JSON.stringify turns into no output at all) is emitted as null.
app.locals.safeJSON = (value: unknown) => escapeJsonForScript(JSON.stringify(value ?? null));

// Attribute escaper for the Open Graph tags in the layouts. Several link
// preview scrapers print "&amp;" literally, so the ampersand is deliberately
// left alone ("Craft & Ship"); the characters that could end the attribute or
// start a tag are escaped (the layouts always use double quotes, so the
// apostrophe is left readable too). A value such as an app name can therefore
// not break out of content="...". An unescaped "&" cannot do that either: at
// worst it forms a character reference that changes how the text reads.
app.locals.ogAttr = (value: unknown) =>
  String(value ?? '')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

// URL helper for href attributes whose value comes from the database; see
// utils/format.ts. Views call it as httpUrl(build.live_url).
app.locals.httpUrl = httpUrl;

// Chat markdown helper: HTML-escape, then render **bold** and newlines.
// Used by server-rendered chat bubbles (e.g. intake-anon.ejs greeting) so
// the markup matches what the client-side JS renderer produces for replies.
app.locals.renderChatMarkdown = (value: unknown) => {
  const s = String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
  return s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
};

// Asset cache buster: git commit SHA or startup timestamp
app.locals.assetVersion = (process.env.RAILWAY_GIT_COMMIT_SHA || process.env.GIT_COMMIT || Date.now().toString()).slice(0, 8);

// USD formatter helper for views: returns "$1,234.56" with comma grouping
// for thousands. Replaces inline `$<%= n.toFixed(2) %>` patterns across the
// admin dashboards so 4-figure spend numbers render with proper formatting.
// Coerces null/undefined/NaN to 0.
app.locals.fmtUsd = (value: unknown, decimals: number = 2) => {
  const n = Number(value);
  const safe = Number.isFinite(n) ? n : 0;
  return '$' + safe.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
};

// Make session data available to all views
app.use((req: Request, res: Response, next: NextFunction) => {
  res.locals.customerEmail = req.session.customerEmail || null;
  res.locals.customerName = req.session.customerName || null;
  res.locals.isLoggedIn = !!req.session.customerId;
  res.locals.isAdmin = isAdminSession(req);
  res.locals.csrfToken = generateCsrfToken(req);
  res.locals.stripePublishableKey = process.env.STRIPE_PUBLISHABLE_KEY || '';
  res.locals.analyticsId = process.env.PLAUSIBLE_DOMAIN || '';
  res.locals.harborEnabled = process.env.CAS_HARBOR_ENABLED === 'true';
  res.locals.canonicalPath = req.path === '/' ? '' : req.path;
  // Per-page OG image (roadmap: seo-per-page-og-images, 2026-05-04). Falls
  // back to /og-default.png for any path not in the curated map. The asset
  // PNGs are pre-generated; see web/src/services/og-images.ts and
  // scripts/generate_og_images.py.
  res.locals.pageOgImage = resolveOgImage(req.path);
  // /lighthouse/* is the keeper-conversion funnel; per founder's spec it must
  // not be SEO-targeted. robots.txt disallows it for compliant crawlers; this
  // meta-tag is the belt-and-suspenders for AI crawlers that ignore robots.txt
  // and for accidental indexation if a /lighthouse URL leaks externally.
  // Honored by layout-harbor.ejs and any view rendering through it.
  res.locals.noIndex = req.path === '/lighthouse' || req.path.startsWith('/lighthouse/');
  // Harbor unified subscription pricing (rev 2026-04-26 redesign).
  // Update both numbers when Stripe subscription billing wires up in Pass R3.
  // See docs/BRAND_VOICE.md.
  res.locals.harborSubscriptionPrice = '$39';
  res.locals.harborSlidingScalePrice = '$19';
  // Default JSON-LD: every Harbor surface ships at least Organization
  // schema. Computed once per request from the canonical helper so the
  // layout cannot drift from organizationJsonLd() output. Routes that
  // need page-specific schema (e.g. /pricing FAQPage) override by passing
  // their own `pageJsonLd` to res.render().
  res.locals.defaultJsonLd = jsonLdScriptTag(
    organizationJsonLd(),
    res.locals.cspNonce as string | undefined
  );
  // Harbor v2 (2026-04-27, rev 2): every customer-facing route defaults to
  // layout-harbor. Admin / ops / api / docs-research / features /
  // export-data / verify-email + the engineering-side `/projects` features
  // tree stay on the original `layout` (those have their own complex tooling
  // UIs the redesign isn't trying to port). Chat-driven /try, /intake, build
  // status, and payment surfaces ARE on Harbor; their views inline the
  // Harbor-flavored chat-bubble CSS where needed.
  const NON_HARBOR_PREFIXES = [
    '/admin', '/ops', '/api', '/webhooks', '/coordinator',
    '/feedback', '/docs/research',
  ];
  const isNonHarbor = NON_HARBOR_PREFIXES.some(p =>
    req.path === p || req.path.startsWith(p + '/')
  );
  if (!isNonHarbor) {
    res.locals.layout = 'layout-harbor';
  }
  // Paint the animated mesh background on public marketing pages.
  // App pages (dashboard, intake, admin) stay clean.
  const meshPaths = new Set([
    '/',
    '/signup',
    '/login',
    '/pricing',
    '/how-it-works',
    '/contact',
    '/gallery',
    '/terms',
    '/privacy',
    '/forgot-password',
    '/reset-password',
  ]);
  res.locals.pageMesh = meshPaths.has(req.path);
  next();
});

/**
 * Read a query or body field as a string. With `extended: true` urlencoded
 * parsing (and with JSON bodies) any field can arrive as an array or object,
 * and calling a string method on one of those throws.
 */
function formString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** Escape text for interpolation into HTML (element content or a quoted attribute). */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// The two public forms below write to the database or send mail without a
// login, so each gets a per-client cap. Counters are in memory (single
// instance); the caps are loose enough that a person never meets them.
const publicFormLimit = (max: number) =>
  boundedRateLimit({
    windowMs: 60 * 60 * 1000,
    max,
    message: 'Too many submissions from this address. Please try again later.',
    standardHeaders: true,
    legacyHeaders: false,
    skip: () => process.env.NODE_ENV === 'test',
    keyGenerator: (req) => rateLimitIpKey(req.ip),
  });
const guideRateLimit = publicFormLimit(20);
const contactRateLimit = publicFormLimit(10);

// Server-side page view logging (fire-and-forget, non-blocking; skipped in test).
// A failed insert never fails the page it belongs to, and it is not swallowed
// either: the first failure in this process is logged with its cause, and
// after that every 500th with the running count, so an unreachable table
// shows up in the log without one line per page view.
let pageViewFailures = 0;
const PAGE_VIEW_FAILURE_LOG_EVERY = 500;
function reportPageViewFailure(err: unknown): void {
  pageViewFailures += 1;
  if (pageViewFailures === 1 || pageViewFailures % PAGE_VIEW_FAILURE_LOG_EVERY === 0) {
    console.error(
      'Page view was not recorded (failure %d in this process): %s',
      pageViewFailures,
      err instanceof Error ? err.message : String(err)
    );
  }
}

// What page views may cost. The table is analytics, and every row is a write
// that an anonymous GET causes, so the writes are bounded twice over:
//
//  - PAGE_VIEWS_PER_ADDRESS_PER_MINUTE rows per client address per minute. A
//    person reading the site stays far under it; a loop from one address is
//    recorded up to the cap and after that only served.
//  - PAGE_VIEWS_PER_MINUTE rows per minute for everyone together, so many
//    addresses at once cannot turn page loads into an open-ended write load.
//
// Past a cap the page is served exactly as before and the view is simply not
// counted, so the numbers undercount during a flood; that is the trade. A
// request for a path that does not exist is still counted like any other (the
// status is not known yet when the row is written), within the same caps. The
// counters are in process memory (per replica, reset on restart) and the
// per-address map is bounded: createMintLimiter sweeps expired entries and
// drops the oldest half when it grows past its key limit.
export const PAGE_VIEWS_PER_ADDRESS_PER_MINUTE = 30;
export const PAGE_VIEWS_PER_MINUTE = 600;
const PAGE_VIEW_WINDOW_MS = 60_000;
const pageViewsByAddress = createMintLimiter({
  max: PAGE_VIEWS_PER_ADDRESS_PER_MINUTE,
  windowMs: PAGE_VIEW_WINDOW_MS,
  maxKeys: 10_000,
});
const pageViewsOverall = createMintLimiter({ max: PAGE_VIEWS_PER_MINUTE, windowMs: PAGE_VIEW_WINDOW_MS });

if (process.env.NODE_ENV !== 'test') {
  app.use((req: Request, _res: Response, next: NextFunction) => {
    if (req.method !== 'GET') return next();
    if (req.path.startsWith('/api/') || req.path.startsWith('/ops/') || req.path.startsWith('/webhooks/')) return next();
    if (req.path === '/health') return next();
    if (/\.(css|js|png|jpg|jpeg|svg|ico|webp|woff2?|ttf|eot|map|json|xml|txt)$/i.test(req.path)) return next();
    // The per-address cap is asked first, so a client that is over its own
    // cap does not use up the shared one.
    if (!pageViewsByAddress.allow(rateLimitIpKey(req.ip)) || !pageViewsOverall.allow('all')) return next();

    const customerId = req.session?.customerId || null;
    const sessionId = req.sessionID || null;
    const utmSource = formString(req.query.utm_source).trim().slice(0, 255) || null;
    const utmMedium = formString(req.query.utm_medium).trim().slice(0, 255) || null;
    const utmCampaign = formString(req.query.utm_campaign).trim().slice(0, 255) || null;
    // The page the visitor came from, as a path: no query string and no
    // fragment (a mailed unsubscribe or reset link carries its address and
    // token there, and a visitor who follows a link out of it sends that URL
    // as the Referer), and no more than loggablePath's bound. The same rule
    // the visit's own path follows.
    const referrer = loggablePath(req.get('referer')) || null;

    pool.query(
      `INSERT INTO page_views (path, utm_source, utm_medium, utm_campaign, referrer, session_id, customer_id)
       VALUES ($1, $2, $3, $4, $5, $6, $7)`,
      [req.path.slice(0, 500), utmSource, utmMedium, utmCampaign, referrer, sessionId, customerId]
    ).catch(reportPageViewFailure);

    next();
  });
}

// Routes
app.use('/', authRouter);
app.use('/intake', intakeRouter);
app.use('/try', anonIntakeRouter);
app.use('/', buildsRouter);
app.use('/webhooks', webhooksRouter);
app.use('/admin', adminRouter);
app.use('/api/support', supportRouter);
app.use('/', featuresRouter);
app.use('/', galleryRouter);
app.use('/ops/api', opsApiRouter);
app.use('/', feedbackRouter);

// CAS Harbor Phase 1: hidden /lighthouse surface, behind feature flag.
// Set CAS_HARBOR_ENABLED=true to mount the routes. Off by default so a
// careless deploy never exposes the surface before it's ready.
if (process.env.CAS_HARBOR_ENABLED === 'true') {
  app.use('/lighthouse', createLighthouseRouter(pool));
  console.log('CAS Harbor: /lighthouse mounted (CAS_HARBOR_ENABLED=true)');
}

// SEO: robots.txt
//
// AI crawler stance: explicitly Allow the major AI search bots (GPTBot,
// ClaudeBot, PerplexityBot, Google-Extended, CCBot, ChatGPT-User,
// OAI-SearchBot, anthropic-ai). The single most-cited cause of "why is my content not getting cited
// by AI search" is robots.txt blocking. We surface the allow-list explicitly
// so a future change to the wildcard rule doesn't accidentally lock us out
// of citation eligibility. Each AI agent gets the same disallow set as the
// default wildcard (admin / api / ops / webhooks).
app.get('/robots.txt', (_req: Request, res: Response) => {
  const aiBots = [
    'GPTBot',          // OpenAI training crawler
    'ChatGPT-User',    // ChatGPT browse-on-demand
    'OAI-SearchBot',   // OpenAI search index
    'ClaudeBot',       // Anthropic crawler
    'anthropic-ai',    // Older Anthropic agent
    'PerplexityBot',   // Perplexity search index
    'Google-Extended', // Google AI (Gemini, AI Overviews) opt-in agent
    'CCBot',           // Common Crawl (feeds many open-source AI models)
  ];
  const aiAllow = aiBots.map(bot => `User-agent: ${bot}\nAllow: /\nDisallow: /admin/\nDisallow: /api/\nDisallow: /ops/\nDisallow: /webhooks/\n`).join('\n');
  res.type('text/plain').send(`# Craft & Ship robots policy
# AI crawlers: explicitly allowed
# Surfaces under /admin, /api, /ops, /webhooks are off-limits to all crawlers.

${aiAllow}
User-agent: *
Allow: /
Disallow: /admin/
Disallow: /api/
Disallow: /ops/
Disallow: /webhooks/

Sitemap: https://craftandship.io/sitemap.xml`);
});

// SEO: sitemap.xml
//
// Curated list of canonical public URLs. Auth pages (/login, /signup) are
// excluded; they don't earn organic traffic and clutter the sitemap.
// Migration / competitor / "alternative to" landing pages do NOT yet exist;
// when they ship, add them here and ensure their visible body copy follows
// the body-vs-meta SEO split.
//
// Each <url> includes a per-URL ISO-8601 lastmod derived from the git-log
// of the corresponding view file (see `services/sitemap-lastmod.ts`). When
// every URL shared a single deploy-date lastmod, Google treated the signal
// as either spam-y or as no useful freshness hint at all; per-view git-log
// gives an accurate value with no extra tracking infrastructure. The dates
// are computed in the background, once per process; this route reads what is
// known and never runs git or touches the disk itself.
app.get('/sitemap.xml', (_req: Request, res: Response) => {
  const urls = [
    '/',
    '/pricing',
    '/the-companion',
    '/how-it-works',
    '/gallery',
    '/about',
    '/about/ocean',
    '/contact',
    '/terms',
    '/privacy',
    '/guide',
    '/guide/read',
    // brand-voice-allow-competitor: /vs comparison hub URL on SEO surface
    '/vs',
    // brand-voice-allow-competitor: SEO comparison page URLs
    ...listComparisonPages().map((p) => `/vs/${p.slug}`),
  ];
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map(u => `  <url><loc>https://craftandship.io${u}</loc><lastmod>${getLastmodForUrl(u)}</lastmod></url>`).join('\n')}
</urlset>`;
  res.type('application/xml').send(xml);
});

// SEO: llms.txt
//
// llms.txt is a no-cost, no-risk extra signal for AI
// crawlers. Real-world citation impact is debated (one analysis of 500
// sites found zero correlation between presence and citation rate), but
// the file is harmless and serves as a quick description of who Craft &
// Ship is for AI engines that do parse it. Keep it short, affirmative,
// forward-looking: same brand-voice rules as customer copy
// (`docs/BRAND_VOICE.md` § 2). Migration / competitor framing does NOT
// belong here; this file is itself a kind of meta surface, but the
// no-defensive-language rule still applies because LLMs paraphrase the
// content into citations users will read.
//
// The build is described as it works: the website is assembled by ordinary
// software from a library of tested building blocks (no model writes the code
// that ships), and every website needs a recorded review approval before it
// goes live. The file does not say who reviews: the code requires that approval
// before a customer can publish, not a particular reviewer.
app.get('/llms.txt', (_req: Request, res: Response) => {
  res.type('text/plain').send(`# Craft & Ship

> Craft & Ship makes custom websites for healers, therapists, breathwork facilitators, event producers, and the practitioners who run their own practices. Each website takes shape in a conversation with Haven, an AI companion who listens and helps you find the words, and is then built from a library of tested building blocks. Every website needs a recorded review approval before it goes live. Yours, intact, kept across the years.

## What we make

- A custom website, built from a library of tested building blocks to fit what you describe.
- Haven, an AI companion who listens during your intake conversation, helps you find the words, and is honest about being AI.
- Hosting and care included, with uptime watched day and night.
- Unlimited revisions and rebuilds, kept across the years.
- A recorded review approval for every website before it goes live.

## How it works

You begin a conversation with Haven. Talk for as long as it feels right. When the shape of your website is clear between you, it is built from a library of tested building blocks, and it needs a recorded review approval before it goes live. When you're ready to make it public, the subscription begins.

## What it costs

Choose any amount from $39 down to $19 a month, whatever fits your practice. One subscription covers everything.

## Who it's for

Healers, somatic therapists, breathwork facilitators, IFS therapists, life coaches, yoga teachers, sound healers, conscious-business owners, event producers, and small studios. Practitioners running their own practices.

## Pages

- Home: https://craftandship.io/
- Pricing: https://craftandship.io/pricing
- The companion (meet Haven): https://craftandship.io/the-companion
- How it works: https://craftandship.io/how-it-works
- Gallery: https://craftandship.io/gallery
- Free guide: https://craftandship.io/guide
- Read the guide: https://craftandship.io/guide/read
- About: https://craftandship.io/about
- About Ocean: https://craftandship.io/about/ocean
- Contact: https://craftandship.io/contact

Sitemap: https://craftandship.io/sitemap.xml
`);
});

// Render helper for the landing page. The page is static copy: it reads
// nothing from the database, so the most requested URL costs no query and
// cannot fail on one. (Customer websites are shown on /gallery, which has its
// own route and reduces the stored screenshots in SQL; see routes/gallery.ts.)
function renderIndexPage(_req: Request, res: Response): void {
  // Harbor v2 home page (/): unified subscription messaging, retired multi-tier framing.
  const harborMetaDescription =
    "Own your website. For healers, therapists, and event producers. " +
    "A custom website shaped in conversation with an AI companion who helps you find the words, " +
    "built from a library of tested building blocks, and it needs a recorded review approval before it goes live. Hosting included. " +
    "Choose any amount from $39 down to $19 a month, whatever fits your practice.";
  res.render('index', {
    layout: 'layout-harbor',
    title: 'Custom Websites + a Companion',
    metaDescription: harborMetaDescription,
  });
}

// Landing page
app.get('/', (req: Request, res: Response) => renderIndexPage(req, res));

// Harbor v2 marketing pages: Living Platform unified offering.
// All four use layout-harbor.ejs (warm-paper Harbor design system).
app.get('/the-companion', (_req: Request, res: Response) => {
  res.render('the-companion', {
    layout: 'layout-harbor',
    title: 'Meet Haven, your companion',
    metaDescription: 'Haven is your companion. She listens, remembers what matters, and is honest about being AI. She is part of every Craft & Ship subscription.',
  });
});

app.get('/about', (_req: Request, res: Response) => {
  // /about ships Organization + Person (Ocean) JSON-LD so AI engines have a
  // canonical entity binding for the studio + the founder. /about/ocean is
  // the dedicated Person page; /about gets both nodes because the studio
  // and the engineer are introduced together here.
  const pageJsonLd = jsonLdScriptTag(
    jsonLdGraph([organizationJsonLd(), oceanPersonJsonLd()]), res.locals.cspNonce as string | undefined);
  res.render('about', {
    layout: 'layout-harbor',
    title: 'About the studio',
    canonicalPath: '/about',
    metaDescription: 'Craft & Ship is a studio in Austin founded by Ocean Longnion. Custom websites for healers, therapists, and event producers, shaped with a thoughtful AI companion, built from tested building blocks, and needs a recorded review approval before they go live.',
    pageJsonLd,
  });
});

app.get('/about/ocean', (_req: Request, res: Response) => {
  // /about/ocean is the founder's page. It carries Organization and Person
  // JSON-LD; each node's `sameAs` list comes from CAS_ORG_SAMEAS and
  // CAS_OCEAN_SAMEAS (services/structured-data.ts) and is left out when unset.
  const pageJsonLd = jsonLdScriptTag(
    jsonLdGraph([organizationJsonLd(), oceanPersonJsonLd()]), res.locals.cspNonce as string | undefined);
  res.render('about-ocean', {
    layout: 'layout-harbor',
    title: 'Ocean Longnion - Senior Web Craftsman',
    canonicalPath: '/about/ocean',
    metaDescription: 'Ocean Longnion is a Senior Web Craftsman and the founder of Craft & Ship, which makes custom websites for healers, therapists, and event producers.',
    pageJsonLd,
  });
});

// /begin: retired 2026-04-27. The dynamic /try (anonymous Haven intake) is
// the canonical entry point; a static form was redundant. Redirect any
// inbound /begin links (mailers, old social posts) to /try.
app.get('/begin', (_req: Request, res: Response) => res.redirect(302, '/try'));

// SEO: /vs comparison surface (roadmap: seo-content-comparison-pages, 2026-05-04).
// Targets the high-intent "alternative to" keyword cluster. Visible body
// stays affirmative per BRAND_VOICE.md; competitor naming lives only in
// title, meta description, URL slug, and JSON-LD entity bindings.
// Data source: web/src/data/comparison-pages.ts (outside brand-voice scope).
// brand-voice-allow-competitor: SEO comparison hub route; visible body is affirmative
app.get('/vs', (_req: Request, res: Response) => {
  const pageJsonLd = jsonLdScriptTag(
    jsonLdGraph([organizationJsonLd(), comparisonHubJsonLd() as Record<string, unknown> & { '@context'?: string }]), res.locals.cspNonce as string | undefined);
  // brand-voice-allow-competitor: hub canonical path on SEO surface
  const HUB_CANONICAL = '/vs';
  // brand-voice-allow-competitor: hub view name carries the SEO slug
  const HUB_VIEW = 'vs-index';
  res.render(HUB_VIEW, {
    layout: 'layout-harbor',
    title: 'A real alternative to template website builders',
    canonicalPath: HUB_CANONICAL,
    metaDescription:
      'Craft & Ship makes custom websites with healers, therapists, breathwork facilitators, and small studios. Built from a library of tested building blocks, with a recorded review approval before it goes live, kept across the years. Hosting and care from $19 to $39 a month.',
    pageJsonLd,
    pages: listComparisonPages(),
  });
});

// One concrete route per comparison page. We use a template-literal route
// path (backticks) so the SEO consistency lint, which scans for quoted
// app.get literals, treats these as data-driven and looks only at the
// sitemap urls array (where the spread above adds them at runtime).
listComparisonPages().forEach((page) => {
  // brand-voice-allow-competitor: SEO comparison page route binding (one per slug)
  app.get(`/vs/${page.slug}`, (_req: Request, res: Response) => {
    const pageJsonLd = jsonLdScriptTag(
      jsonLdGraph([organizationJsonLd(), comparisonPageJsonLd(page) as Record<string, unknown> & { '@context'?: string }]), res.locals.cspNonce as string | undefined);
    // brand-voice-allow-competitor: canonicalPath surfaces the competitor slug
    const canonicalPath = `/vs/${page.slug}`;
    res.render('comparison', {
      layout: 'layout-harbor',
      title: page.pageTitle,
      canonicalPath,
      metaDescription: page.metaDescription,
      pageJsonLd,
      page,
    });
  });
});

// Free guide: /guide (landing) + /guide/read (full text)
const GUIDE_EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const guideRenderOpts = (overrides: Record<string, unknown> = {}) => ({
  layout: 'layout-harbor',
  title: "The Conscious Practitioner's Website Liberation Guide",
  canonicalPath: '/guide',
  metaDescription:
    'A free guide for practitioners on digital ownership, platform lock-in, and building a website that is truly yours. By Ocean Longnion, Senior Web Craftsman.',
  ...overrides,
});

app.get('/guide', (_req: Request, res: Response) => {
  res.render('guide', guideRenderOpts());
});

app.get('/guide/read', (_req: Request, res: Response) => {
  res.render('guide-read', {
    layout: 'layout-harbor',
    title: "The Quiet Rebellion: 5 Ways a Website That's Truly Yours Frees Your Practice",
    canonicalPath: '/guide/read',
    metaDescription:
      'Five parts on platform lock-in, real ownership, why a website that fits one practice communicates trust, how to move without panic, and what freedom feels like. By Ocean Longnion, Senior Web Craftsman.',
  });
});

app.post('/guide', guideRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const emailVal = formString((req.body || {}).email).trim().toLowerCase().slice(0, 255);
  if (!emailVal || !GUIDE_EMAIL_RE.test(emailVal)) {
    return res.render('guide', guideRenderOpts({ guideError: 'Please enter a valid email address.', formEmail: emailVal }));
  }
  try {
    await pool.query(
      `INSERT INTO newsletter_subscribers (email, source)
       VALUES ($1, $2)
       ON CONFLICT (email) DO UPDATE
         SET subscribed_at = EXCLUDED.subscribed_at
         WHERE newsletter_subscribers.unsubscribed_at IS NULL`,
      [emailVal, 'guide']
    );
  } catch (err) {
    // The address was not stored. Say so, keep what was typed, and use a 5xx
    // status so monitoring sees a failing form and not a success (the contact
    // form below does the same). The guide itself needs no sign-up, and the
    // page still links to it.
    console.error('[guide-signup] db insert failed:', (err as Error).message);
    return res.status(503).render(
      'guide',
      guideRenderOpts({
        guideError: 'Your address could not be added to the list just now. Please try again in a little while.',
        formEmail: emailVal,
      })
    );
  }
  console.info('[guide-signup] subscriber recorded');
  return res.render('guide', guideRenderOpts({ submitted: true }));
});

// Pricing page (Harbor v2: unified subscription)
app.get('/pricing', (_req: Request, res: Response) => {
  // Build the FAQ list once; pass it to the view (for the visible accordion)
  // and to the JSON-LD FAQPage schema so the two never drift apart.
  const faqs = pricingFaqs({
    subscription: res.locals.harborSubscriptionPrice as string,
    sliding: res.locals.harborSlidingScalePrice as string,
  });
  const pageJsonLd = jsonLdScriptTag(
    jsonLdGraph([
      organizationJsonLd(),
      faqPageJsonLd(faqs),
    ]), res.locals.cspNonce as string | undefined);
  res.render('pricing', {
    layout: 'layout-harbor',
    title: 'Pricing',
    canonicalPath: '/pricing',
    metaDescription: 'One subscription. Choose any amount from $39 down to $19 a month, whatever fits your practice. Includes a custom website built from a library of tested building blocks, hosting and care, an AI companion, unlimited revisions and rebuilds kept across the years, and a recorded review approval for every website before it goes live.',
    pricingFaqs: faqs,
    pageJsonLd,
  });
});

// Legal pages. Every figure the two pages print about what this application
// does (how long a cookie lasts, how many builds may run) is handed to the
// view from the constant the code itself uses, so the page and the behaviour
// cannot drift apart. views.marketing-accuracy.test.ts holds the page to
// them, and to what account deletion removes and leaves.
const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

/** The cookies this application sets, as the privacy page states them. */
export function privacyCookieFacts(): {
  sessionCookieName: string;
  sessionIdleHours: number;
  sessionMaxDays: number;
  conversationCookieName: string;
  conversationDays: number;
  conversationPaidDays: number;
} {
  return {
    sessionCookieName: SESSION_COOKIE_NAME,
    sessionIdleHours: SESSION_IDLE_TIMEOUT_MS / HOUR_MS,
    sessionMaxDays: SESSION_ABSOLUTE_MAX_MS / DAY_MS,
    conversationCookieName: TRIAL_COOKIE_NAME,
    conversationDays: TRIAL_TTL_DAYS,
    conversationPaidDays: PAID_TRIAL_ACCESS_DAYS,
  };
}

app.get('/terms', (_req: Request, res: Response) => {
  res.render('terms', {
    title: 'Terms of Service',
    buildLimits: {
      active: MAX_ACTIVE_BUILDS_PER_CUSTOMER,
      startsPerDay: MAX_BUILD_STARTS_PER_DAY,
      retriesPerBuild: MAX_MANUAL_RETRIES_PER_BUILD,
    },
  });
});

app.get('/privacy', (_req: Request, res: Response) => {
  res.render('privacy', {
    title: 'Privacy Policy',
    cookieFacts: privacyCookieFacts(),
  });
});

app.get('/how-it-works', (_req: Request, res: Response) => {
  res.render('how-it-works', {
    title: 'How It Works',
    metaDescription: 'See the technology behind Craft & Ship: Node.js, PostgreSQL, automated testing, and 24/7 monitoring. Real code you own, hosted anywhere you like.',
  });
});

app.get('/docs/research', (_req: Request, res: Response) => {
  res.render('docs-research', {
    title: 'Research & Sources',
    // brand-voice-allow-competitor: this is the internal research-citations index; competitor names are the page's purpose. Customer-facing surfaces still hold rule 2.
    metaDescription: 'Every pricing claim on Craft & Ship traces back to a public source. Vendor pricing pages, third-party teardowns, contract terms, and review corpora for Mindbody, Acuity, Vagaro, and Booker.',
  });
});

app.get('/contact', (req: Request, res: Response) => {
  res.render('contact', {
    title: 'Contact',
    metaDescription: 'Get in touch with Ocean Longnion at Craft & Ship. Questions about your website or pricing: send a message through the contact form.',
    customerEmail: req.session.customerEmail || null,
    customerName: req.session.customerName || null,
  });
});

// Keys are the <option> values of the contact form (views/contact.ejs). The
// last three are options an earlier version of the form offered; they are
// kept so a cached copy of that page still files under the right label.
const CONTACT_SUBJECT_LABELS: Record<string, string> = {
  general: 'General question',
  project: 'Project outside the standard offer',
  partnership: 'Partnership or collaboration',
  press: 'Press or interview',
  grant: 'Grant program',
  other: 'Something else',
  pricing: 'Pricing & plans',
  build: 'My app build',
  feature: 'Feature request',
};
const DEFAULT_CONTACT_FROM = 'Craft & Ship <noreply@craftandship.io>';

/**
 * Build the notification email for a contact-form message. Every visitor
 * supplied value is HTML-escaped, and the subject line is assembled from an
 * allow-listed label plus the sender name with control characters removed, so
 * nothing the visitor types can add markup to the email or a second line to a
 * header. Exported for tests.
 */
export function buildContactEmail(input: { name: string; email: string; subjectKey: string; message: string }): {
  subject: string;
  html: string;
  replyTo: string;
} {
  const subjectLabel = CONTACT_SUBJECT_LABELS[input.subjectKey] || CONTACT_SUBJECT_LABELS.general;
  const headerSafeName = input.name.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
  return {
    subject: `[Contact] ${subjectLabel}: ${headerSafeName}`,
    replyTo: input.email,
    html: `<p><strong>From:</strong> ${escapeHtml(input.name)} &lt;${escapeHtml(input.email)}&gt;</p>
<p><strong>Subject:</strong> ${escapeHtml(subjectLabel)}</p>
<hr/>
<p style="white-space:pre-wrap">${escapeHtml(input.message)}</p>`,
  };
}

type ContactSendOutcome = 'sent' | 'failed' | 'unconfigured';

/**
 * How long the contact form waits for the mail provider. The visitor's
 * request is held open for the send, so the send has a deadline: at the
 * deadline the request is aborted and the page says the note was not sent.
 * (Only the status of the provider's answer is read, never its body.)
 */
export const CONTACT_SEND_TIMEOUT_MS = 15_000;

/**
 * Email a contact-form message to ADMIN_EMAIL through Resend. Posts straight to
 * the provider and writes no email_log row. 'unconfigured' means there is no
 * mail transport (RESEND_API_KEY or ADMIN_EMAIL unset, expected in local
 * development); 'failed' means the provider refused the message or could not
 * be reached. The message body is not written to the log.
 */
async function sendContactEmail(input: { name: string; email: string; subjectKey: string; message: string }): Promise<ContactSendOutcome> {
  const apiKey = process.env.RESEND_API_KEY;
  const adminEmail = process.env.ADMIN_EMAIL || '';
  if (!apiKey || !adminEmail) {
    console.log('[contact] RESEND_API_KEY or ADMIN_EMAIL not set; contact message was not sent.');
    return 'unconfigured';
  }
  try {
    const mail = buildContactEmail(input);
    const response = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from: process.env.RESEND_FROM_EMAIL || DEFAULT_CONTACT_FROM,
        to: [adminEmail],
        reply_to: mail.replyTo,
        subject: mail.subject,
        html: mail.html,
      }),
      signal: AbortSignal.timeout(CONTACT_SEND_TIMEOUT_MS),
    });
    if (!response.ok) {
      console.error(`[contact] email send failed: Resend answered HTTP ${response.status}`);
      return 'failed';
    }
    return 'sent';
  } catch (err) {
    console.error('[contact] email send error:', (err as Error).message);
    return 'failed';
  }
}

app.post('/contact', contactRateLimit, validateCsrfToken, async (req: Request, res: Response) => {
  const body = (req.body || {}) as Record<string, unknown>;

  const nameVal = formString(body.name).trim().slice(0, 200);
  const emailVal = formString(body.email).trim().slice(0, 255);
  const subjectRaw = formString(body.subject).trim();
  const subjectVal = Object.prototype.hasOwnProperty.call(CONTACT_SUBJECT_LABELS, subjectRaw) ? subjectRaw : 'general';
  const messageVal = formString(body.message).trim().slice(0, 5000);

  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  if (!nameVal || !emailVal || !messageVal || !EMAIL_RE.test(emailVal)) {
    // The same page with the typed values kept, answered 400 like the other
    // forms (/signup, /reset-password), so a client or a monitor that reads
    // the status sees a form that was not accepted. Nothing reads this
    // answer as a success: the page is plain HTML and the form has no script.
    return res.status(400).render('contact', {
      title: 'Contact',
      error: 'Please fill in all fields with a valid email address.',
      formValues: { name: nameVal, email: emailVal, subject: subjectVal, message: messageVal },
      customerEmail: req.session.customerEmail || null,
      customerName: req.session.customerName || null,
    });
  }

  const outcome = await sendContactEmail({ name: nameVal, email: emailVal, subjectKey: subjectVal, message: messageVal });
  if (outcome === 'sent') {
    return res.render('contact', {
      title: 'Contact',
      successMessage: 'Thank you. Your note has been sent.',
      customerEmail: req.session.customerEmail || null,
      customerName: req.session.customerName || null,
    });
  }

  // The note did not reach Ocean. Say so, keep what the visitor typed, and use
  // a 5xx status so monitoring sees a failing form rather than a success. A
  // missing mail configuration is named for a developer and withheld from
  // visitors in production.
  const error = outcome === 'unconfigured' && isDevOrTestEnv()
    ? 'Your note could not be sent: mail is not configured on this server (set RESEND_API_KEY and ADMIN_EMAIL), so nothing was sent.'
    : 'Your note could not be sent. Please try again in a little while.';
  return res.status(outcome === 'unconfigured' ? 503 : 502).render('contact', {
    title: 'Contact',
    error,
    formValues: { name: nameVal, email: emailVal, subject: subjectVal, message: messageVal },
    customerEmail: req.session.customerEmail || null,
    customerName: req.session.customerName || null,
  });
});

// Health check with DB ping, memory, and pool stats
app.get('/health', async (_req: Request, res: Response) => {
  try {
    const start = Date.now();
    await pool.query('SELECT 1');
    const dbLatencyMs = Date.now() - start;
    const mem = process.memoryUsage();

    // Worker heartbeat: last startup time and git commit
    let workerHeartbeat: { started_at: string; git_commit: string } | null = null;
    try {
      const hbResult = await pool.query('SELECT started_at, git_commit FROM worker_heartbeat WHERE id = 1');
      if (hbResult.rows.length > 0) {
        workerHeartbeat = {
          started_at: hbResult.rows[0].started_at,
          git_commit: hbResult.rows[0].git_commit || '',
        };
      }
    } catch {
      // Table may not exist yet; ignore
    }

    res.json({
      status: 'ok',
      version: appVersion,
      git_commit: process.env.RAILWAY_GIT_COMMIT_SHA || process.env.GIT_COMMIT || '',
      worker: workerHeartbeat,
      db: 'connected',
      db_latency_ms: dbLatencyMs,
      uptime_seconds: Math.floor(process.uptime()),
      memory: {
        rss_mb: Math.round(mem.rss / 1024 / 1024),
        heap_used_mb: Math.round(mem.heapUsed / 1024 / 1024),
        heap_total_mb: Math.round(mem.heapTotal / 1024 / 1024),
      },
      pool: {
        total: (pool as any).totalCount ?? 0,
        idle: (pool as any).idleCount ?? 0,
        waiting: (pool as any).waitingCount ?? 0,
      },
    });
  } catch {
    res.status(503).json({
      status: 'degraded',
      db: 'unreachable',
      uptime_seconds: Math.floor(process.uptime()),
    });
  }
});

// 404 handler. A script gets JSON, a browser the page (utils/json-request.ts).
app.use((req: Request, res: Response) => {
  if (isJsonRequest(req)) {
    sendJsonError(res, 404);
    return;
  }
  res.status(404).render('404', { title: 'Page Not Found' });
});

// Sentry error handler: must be after routes, before our error handler
Sentry.setupExpressErrorHandler(app);

// Error handler
app.use((err: Error & { status?: number; statusCode?: number }, req: Request, res: Response, next: NextFunction) => {
  // A response is already on the wire: nothing useful can be rendered. Hand
  // the error to Express's default handler, which closes the connection.
  if (res.headersSent) {
    next(err);
    return;
  }

  // Whether the caller is a script or a page decides the shape of every
  // answer below: JSON for a JSON request, the HTML page for everything else
  // (isJsonRequest, utils/json-request.ts).
  const wantsJson = isJsonRequest(req);

  // Client errors raised by middleware (malformed JSON body, body over the
  // size limit, bad percent-encoding) carry a 4xx status. They are the
  // client's problem, not a server fault: answer with that status and keep
  // them out of the error log and the diagnostics buffer.
  const clientStatus = err.status ?? err.statusCode;
  if (typeof clientStatus === 'number' && Number.isInteger(clientStatus) && clientStatus >= 400 && clientStatus < 500) {
    if (wantsJson) {
      sendJsonError(res, clientStatus);
      return;
    }
    renderErrorPage(res, clientStatus);
    return;
  }

  console.error('Unhandled error:', err.message, err.stack);

  // Extra warning for session store errors
  const errMsg = err.message || '';
  if (errMsg.includes('session') || errMsg.includes('connect-pg-simple') || errMsg.includes('user_sessions')) {
    console.error('SESSION STORE ERROR: check user_sessions table exists with correct schema');
  }

  // The path only: the query string is where reset, verification and
  // unsubscribe links carry their token, and GET /ops/api/v1/errors returns
  // these entries. (recordRecentError strips it again whatever it is given.)
  recordRecentError({
    timestamp: new Date().toISOString(),
    method: req.method,
    path: loggablePath(req.originalUrl),
    message: err.message,
    stack: err.stack?.split('\n').slice(0, 5).join('\n'),
    sessionInfo: req.session ? {
      customerId: req.session.customerId || null,
      customerEmail: req.session.customerEmail || null,
    } : undefined,
  });
  const publicMessage = showErrorDetail ? err.message : 'An unexpected error occurred.';
  if (wantsJson) {
    sendJsonError(res, 500, publicMessage);
    return;
  }
  res.status(500).render('500', {
    title: 'Server Error',
    error: publicMessage,
  });
});

export default app;
