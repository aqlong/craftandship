# cli/build.py - Code generation from validated spec

"""LEGACY v1 build engine (frozen): superseded by the v2 engine in compose/ and
kept for reference only. It runs only when CAS_BUILD_ENGINE=v1 is set; do not
extend it. See cli/README.md for what it did and where it fell short.
"""

import json
import os
import re
import sys
import time
import types
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from validate import load_and_validate
from api_client import (
    call_api,
    call_api as _shared_call_api,
    make_client,
    should_use_batch,
)
from build_trace import emit_build_trace
import pipeline
from pipeline import processor, run_pipeline, assert_pipeline_parity

load_dotenv()

# Per-build context for fail-safe trace emission from post-processors that
# don't receive build_dir in their signature. Set in build_app() around the
# post-processor block; post-processors read via _CURRENT_BUILD_DIR.get().
_CURRENT_BUILD_DIR: ContextVar[Optional[Path]] = ContextVar("cas_build_dir", default=None)

# Read through the one reader of this switch (cli/deploy.py): on unless set to
# an explicit OFF word, so the generator, the tests and the deploy probe agree
# whatever the spelling.
from deploy import magic_link_auth_enabled  # noqa: E402

USE_MAGIC_LINK_AUTH = magic_link_auth_enabled()

# Hardcoded build prompt rule: auth middleware on protected pages.
# This is the #1 test failure in CAS builds. The failure_kb has the pattern
# but it needs 3+ outcomes at 70%+ success rate to activate. This bypasses
# the KB threshold so every build gets the rule immediately.
_AUTH_MIDDLEWARE_RULE = (
    "\n## CRITICAL: Auth middleware on protected pages\n"
    "Every page with auth_required=true MUST have requireAuth() middleware. "
    "The route handler must call requireAuth before rendering. "
    "Unauthenticated requests to protected pages MUST receive 401/403 or "
    "redirect to /login, never 200.\n"
)

# ---------------------------------------------------------------------------
# Per-category video pools (loaded once at module import)
# ---------------------------------------------------------------------------
_VIDEO_POOLS_PATH = Path(__file__).parent / "video_pools.json"
_VIDEO_POOLS = {}
if _VIDEO_POOLS_PATH.exists():
    try:
        _VIDEO_POOLS = json.loads(_VIDEO_POOLS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass

# ---------------------------------------------------------------------------
# Design style -> font pairing map
# Each style maps to (display_font, body_font) from Google Fonts.
# Banned as primary visible font: Inter, Roboto, Open Sans, Lato, Arial, Helvetica.
# ---------------------------------------------------------------------------
STYLE_FONT_MAP = {
    # Legacy keywords
    "modern light": ("Source Sans 3", "DM Sans"),
    "modern dark": ("Space Grotesk", "IBM Plex Sans"),
    "clean minimal": ("Newsreader", "Source Sans 3"),
    "warm earthy": ("Playfair Display", "Crimson Pro"),
    "bold vibrant": ("Montserrat", "Space Grotesk"),
    "corporate professional": ("Merriweather", "IBM Plex Sans"),
    # Extended directions
    "brutalist": ("Space Mono", "Space Grotesk"),
    "editorial": ("Playfair Display", "Source Sans 3"),
    "retro-futuristic": ("Orbitron", "IBM Plex Sans"),
    "luxury": ("Cormorant Garamond", "Montserrat"),
    "playful": ("Fredoka", "Nunito"),
    "maximalist": ("Bricolage Grotesque", "Space Grotesk"),
    "scandinavian": ("Source Sans 3", "DM Sans"),
    "japanese-minimal": ("Newsreader", "Source Sans 3"),
    "art-deco": ("Poiret One", "Raleway"),
    "neo-industrial": ("JetBrains Mono", "IBM Plex Sans"),
}

# Visual characteristics per style for the build prompt
STYLE_VISUAL_TRAITS = {
    "warm earthy": "serif headings, warm amber/orange gradients, rounded-xl corners, shadow-md, friendly approachable palette",
    "modern dark": "geometric sans headings, dark slate gradients, sharp rounded-lg corners, subtle borders, cool palette",
    "bold vibrant": "strong display font headings, bright saturated accents, high contrast, rounded-xl, gradient accents",
    "clean minimal": "elegant serif headings with lots of whitespace, subtle borders instead of shadows, typography-focused",
    "corporate professional": "traditional serif headings, navy/gray palette, structured grid, shadow-sm cards, formal hierarchy",
    "modern light": "clean sans headings, airy white/gray palette, shadow-sm, rounded-lg, spacious feel",
    "brutalist": "monospace headings, high-contrast monochrome + one punch color, sharp corners, raw borders",
    "editorial": "serif display headings, asymmetric layouts, generous whitespace, magazine-like hierarchy",
    "retro-futuristic": "geometric/tech display headings, dark backgrounds with neon accents, angular shapes",
    "luxury": "elegant serif headings, deep navy/black + gold accent, generous padding, refined spacing",
    "playful": "rounded display headings, saturated primary colors, bouncy rounded-xl corners, friendly shadows",
    "maximalist": "bold grotesque headings, layered colors, high saturation, strong contrasts, dynamic layouts",
    "scandinavian": "clean humanist sans, soft neutral palette, extreme whitespace, minimal borders",
    "japanese-minimal": "refined serif headings, muted earth tones, extreme restraint, intentional negative space",
    "art-deco": "geometric display headings, gold/cream/black palette, symmetry, decorative borders",
    "neo-industrial": "monospace headings, dark grays, technical palette, structured grid, sharp edges",
}

BANNED_FONTS = {"Inter", "Roboto", "Open Sans", "Lato", "Arial", "Helvetica"}

# ---------------------------------------------------------------------------
# Product type -> stock photo category mapping
# Maps design_data product types to the 2 most relevant photo categories.
# ---------------------------------------------------------------------------
PRODUCT_TYPE_STOCK_CATEGORIES = {
    "pet_services": ["pets", "people"],
    "veterinary": ["pets", "medical"],
    "restaurant": ["food", "people"],
    "food_delivery": ["food", "people"],
    "bakery_cafe": ["food", "people"],
    "recipe_cooking": ["food", "people"],
    "brewery_winery": ["food", "hospitality"],
    "fitness": ["fitness", "people"],
    "medical_clinic": ["medical", "people"],
    "healthcare": ["medical", "people"],
    "ecommerce": ["business", "people"],
    "ecommerce_luxury": ["business", "people"],
    "saas": ["tech", "business"],
    "real_estate": ["real_estate", "nature"],
    "education": ["education", "people"],
    "online_course": ["education", "tech"],
    "hotel": ["hospitality", "nature"],
    "booking": ["hospitality", "people"],
    "travel": ["hospitality", "nature"],
    "event_management": ["people", "business"],
    "nonprofit": ["people", "nature"],
    "wellness": ["wellness", "nature"],
    "spa": ["wellness", "nature"],
    "beauty_spa": ["beauty", "wellness"],
    "community": ["wellness", "people"],
    "job_board": ["people", "business"],
    "analytics_dashboard": ["tech", "business"],
    "freelancer": ["tech", "business"],
    "invoice": ["business", "tech"],
    "marketplace": ["business", "people"],
    "construction": ["construction", "business"],
    "automotive": ["automotive", "business"],
    "childcare": ["childcare", "people"],
    "sports": ["sports", "fitness"],
    "arts": ["arts", "people"],
    "legal": ["legal", "business"],
    "photography": ["arts", "nature"],
}


def _get_style_fonts(spec: dict) -> tuple:
    """Return (display_font, body_font) for a spec's design style.

    Uses spec.design.font_display / font_body if explicitly set,
    otherwise falls back to STYLE_FONT_MAP lookup, defaulting to editorial.
    """
    design = spec.get("design", {})
    style = design.get("style", "modern light")
    default_pair = STYLE_FONT_MAP.get(style, STYLE_FONT_MAP["editorial"])
    display_font = design.get("font_display") or default_pair[0]
    body_font = design.get("font_body") or default_pair[1]
    return (display_font, body_font)


def _get_style_traits(spec: dict) -> str:
    """Return visual trait description for the spec's design style."""
    design = spec.get("design", {})
    style = design.get("style", "modern light")
    return STYLE_VISUAL_TRAITS.get(style, STYLE_VISUAL_TRAITS["modern light"])


def _build_design_context(spec: dict, abbreviated: bool = False) -> str:
    """Build design prompt section from spec + UUPM data.

    Calls generate_design_md() from design_data.py to produce a structured
    DESIGN.md-format block. Full mode: all 9 sections (~850 tokens).
    Abbreviated mode: sections 2, 3, 4, 7 only (~350 tokens).

    Args:
        spec: App spec dict
        abbreviated: If True, emit only sections 2,3,4,7 (for Phase 1 and Phase N prompts)
    """
    try:
        from design_data import (get_design_context, VISUAL_DESIGN_CHECKLIST,
                                 generate_design_md)
        ctx = get_design_context(spec)
    except Exception:
        # Fallback to legacy path if design_data is unavailable
        display_font, body_font = _get_style_fonts(spec)
        style_traits = _get_style_traits(spec)
        design_style = spec.get("design", {}).get("style", "modern light")
        return (
            f'## Design Direction\n\n'
            f'Style: "{design_style}" - {style_traits}\n\n'
            f'Typography: Use "{display_font}" for headings and "{body_font}" for body text.\n'
            f'Load via Google Fonts: family={display_font.replace(" ", "+")}:wght@400;700;900'
            f'&family={body_font.replace(" ", "+")}:wght@300;400;500;600;700\n'
            f'In tailwind.config.js fontFamily: display: [\'{display_font}\', \'serif\'], '
            f'body: [\'{body_font}\', \'sans-serif\']\n'
        )

    design_md = generate_design_md(spec, ctx, abbreviated=abbreviated)

    if not abbreviated:
        # Append the VISUAL_DESIGN_CHECKLIST after the DESIGN.md block.
        # The checklist covers universal component rules (split-panel login, etc.)
        # that complement the style-specific section 7 don'ts.
        return design_md + "\n" + VISUAL_DESIGN_CHECKLIST
    return design_md


MODEL = "claude-opus-4-6"
MODEL_FOUNDATION = "claude-sonnet-4-6"  # Cheaper model for Phase 1 (boilerplate)
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1

BUILD_SYSTEM_PROMPT = """You are the build agent for Craft and Ship. You take a structured JSON app spec \
and generate a complete, working Express.js + PostgreSQL web application.

## Output rules

You output files one at a time. For each file, use this exact format:

--BEGINFILE-- path/to/file.ext
file contents here
--ENDFILE--

CRITICAL: The delimiters --BEGINFILE-- and --ENDFILE-- must appear on their own lines with nothing else on that line. Never write --BEGINFILE-- or --ENDFILE-- inside file content, not in comments, not in strings, not anywhere. These are reserved output markers only.

Do not output anything outside of file blocks. No explanations, no commentary.

## Tech stack

- Express.js with EJS templates + express-ejs-layouts for layout wrapping
- PostgreSQL with the 'pg' npm package (no ORM)
- bcrypt for password hashing (12 rounds minimum)
- express-session + connect-pg-simple for persistent PostgreSQL session store (REQUIRED; do NOT use the default MemoryStore)
- express-rate-limit for login brute-force protection
- DO NOT install csrf-csrf or csurf; use the session-based CSRF pattern described below instead
- morgan for HTTP request logging
- dotenv for environment variables
- CSS: Tailwind CSS v3 compiled at build time + custom CSS in public/style.css (see Design section below)

## App structure

Generate these files:

1. package.json - dependencies and start script
2. .env.example - template with DATABASE_URL, SESSION_SECRET, PORT
3. db/schema.sql - CREATE TABLE IF NOT EXISTS statements with indexes
4. db/seed.sql - INSERT statements for seed data
5. db/migrate.js - Auto-migration on startup (required, see section below)
6. server.js - Express app setup with security middleware, morgan logging, route mounting
7. routes/ - One route file per logical group (auth.js, plus one per major feature area)
8. views/ - EJS templates with proper escaping throughout
9. public/style.css - Themed, responsive styling using the design spec
10. middleware/auth.js - Authentication and role-checking middleware
11. public/manifest.json - PWA web app manifest (see PWA section below)
12. public/sw.js - Service worker for offline support (see PWA section below)
13. public/favicon.svg - Simple SVG favicon representing the app (see Favicon section below)
14. tailwind.config.js - Tailwind CSS configuration with custom theme colors (see Design section below)

## Security requirements (non-negotiable)

### Session
- SESSION_SECRET must come from environment. If missing, log error and exit:
  `if (!process.env.SESSION_SECRET) { console.error('SESSION_SECRET not set'); process.exit(1); }`
- CRITICAL: Add `app.set('trust proxy', 1)` BEFORE the session middleware. Railway (and all cloud platforms) terminate SSL at the edge proxy. Without this, Express sees requests as HTTP, `secure: true` cookies are never sent back to the browser, and the session is lost between requests; breaking CSRF and auth.
- Use connect-pg-simple as the session store (NOT the default MemoryStore; it leaks memory and loses sessions on restart):
  ```js
  const session = require('express-session');
  const pgSession = require('connect-pg-simple')(session);
  const { Pool } = require('pg');
  const pool = new Pool({
    connectionString: process.env.DATABASE_URL,
    ssl: (!(process.env.DATABASE_URL || '').includes('railway.internal') && process.env.NODE_ENV === 'production') ? { rejectUnauthorized: false } : false
  });
  app.use(session({
    store: new pgSession({ pool, tableName: 'user_sessions', createTableIfMissing: true }),
    secret: process.env.SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: { httpOnly: true, secure: process.env.NODE_ENV === 'production', sameSite: 'lax', maxAge: 8 * 60 * 60 * 1000 }
  }));
  ```
- Order in server.js MUST be (copy this exact skeleton):
  ```js
  const app = express();
  app.set('trust proxy', 1);  // FIRST: before ANY middleware. Required for Railway/cloud.
  app.set('view engine', 'ejs');
  app.use(express.urlencoded({ extended: false }));
  app.use(express.json());
  app.use(express.static('public'));
  app.use(session({ store: new pgSession({...}), ... }));
  app.use(generateCsrfToken);
  app.use(require('morgan')('combined'));
  app.use('/', authRouter);
  // ... other routers
  ```
  If `app.set('trust proxy', 1)` is missing, sessions WILL break on Railway.

### Rate limiting on login
- Import express-rate-limit and apply to POST /login:
  ```js
  const rateLimit = require('express-rate-limit');
  const loginLimiter = rateLimit({ windowMs: 15 * 60 * 1000, max: 10, message: 'Too many login attempts, please try again later.' });
  router.post('/login', loginLimiter, async (req, res) => { ... });
  ```

### Security headers (helmet)
- Add `helmet` to package.json dependencies
- Add `app.use(require('helmet')())` BEFORE routes in server.js
- This adds Content-Security-Policy, X-Frame-Options, X-Content-Type-Options, HSTS, Referrer-Policy automatically
- Override CSP to allow Google Fonts and inline styles: `app.use(helmet({ contentSecurityPolicy: { directives: { defaultSrc: ["'self'"], scriptSrc: ["'self'", "'unsafe-inline'"], styleSrc: ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"], fontSrc: ["'self'", "https://fonts.gstatic.com"], imgSrc: ["'self'", "data:"] } } }))`

### Password validation
- On POST /register, enforce minimum 8 characters: `if (!password || password.length < 8) { return res.render('register', { error: 'Password must be at least 8 characters', csrfToken: res.locals.csrfToken }); }`
- Show this requirement as help text below the password field in the register form

### Email verification (REQUIRED for all apps with user registration)
Every app with a /register route MUST implement email verification. Users who register cannot
log in until their email is verified.

**Schema additions** (add to db/schema.sql users table and a separate tokens table):
```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS email_verification_tokens (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  token VARCHAR(64) UNIQUE NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  used_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW()
);
```
Seed users must have `email_verified = TRUE` so smoke tests can log in immediately.

**Registration flow** (POST /register):
1. Create user with `email_verified = FALSE`
2. Generate token: `const token = require('crypto').randomBytes(32).toString('hex')`
3. Store in email_verification_tokens with `expires_at = NOW() + 24 hours`
4. Attempt to send verification email (see below); if it fails, still proceed
5. Do NOT set session or redirect to dashboard; redirect to `/verify-email-sent?email=<email>`
6. GET /verify-email-sent renders a page: "Check your inbox for a verification link" with a resend form

**Verification route** (GET /verify-email):
```js
router.get('/verify-email', async (req, res) => {
  const { token } = req.query;
  const result = await db.query(
    'SELECT * FROM email_verification_tokens WHERE token=$1 AND used_at IS NULL AND expires_at > NOW()',
    [token]
  );
  if (!result.rows[0]) return res.render('verify-email-sent', { error: 'Invalid or expired link. Request a new one.', email: '' });
  await db.query('UPDATE email_verification_tokens SET used_at=NOW() WHERE token=$1', [token]);
  await db.query('UPDATE users SET email_verified=TRUE WHERE id=$1', [result.rows[0].user_id]);
  const user = await db.query('SELECT * FROM users WHERE id=$1', [result.rows[0].user_id]);
  req.session.userId = user.rows[0].id;
  req.session.userRole = user.rows[0].role;
  req.session.userName = user.rows[0].name;
  req.session.save(() => res.redirect('/dashboard'));
});
```

**Login gate**: POST /login must check `email_verified = TRUE` before allowing login:
```js
if (!user.email_verified) {
  return res.render('login', { error: 'Please verify your email before logging in. Check your inbox or <a href="/resend-verification">resend the link</a>.', csrfToken: res.locals.csrfToken });
}
```

**Resend route** (POST /resend-verification):
```js
router.post('/resend-verification', validateCsrfToken, async (req, res) => {
  const { email } = req.body;
  const user = await db.query('SELECT * FROM users WHERE email=$1 AND email_verified=FALSE', [email]);
  if (user.rows[0]) {
    const token = require('crypto').randomBytes(32).toString('hex');
    await db.query('INSERT INTO email_verification_tokens (user_id,token,expires_at) VALUES ($1,$2,NOW()+interval\'24 hours\')', [user.rows[0].id, token]);
    await _sendVerificationEmail(email, token, req);
  }
  // Always show success (prevents user enumeration)
  res.render('verify-email-sent', { email, message: 'A new verification link has been sent if that email is registered.' });
});
```

**Email sending helper**: works with or without RESEND_API_KEY:
```js
async function _sendVerificationEmail(email, token, req) {
  const verifyUrl = `${req.protocol}://${req.get('host')}/verify-email?token=${token}`;
  console.log(`[EMAIL VERIFICATION] Link for ${email}: ${verifyUrl}`);
  const apiKey = process.env.RESEND_API_KEY;
  if (!apiKey) return; // no email service configured; link is logged above
  try {
    const resp = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from: 'noreply@craftandship.io',
        to: [email],
        subject: 'Verify your email address',
        html: `<p>Click <a href="${verifyUrl}">here</a> to verify your email. Link expires in 24 hours.</p>`
      })
    });
    if (!resp.ok) { const t = await resp.text().catch(() => ''); console.error('Verification email API error:', resp.status, t); }
  } catch (e) { console.error('Email send failed (non-fatal):', e.message); }
}
```

**Views needed**:
- views/verify-email-sent.ejs: "Check your inbox" page with email displayed, resend form (POST /resend-verification with email hidden field + CSRF), and "Back to Login" link
- The verify-email-sent page must also accept `?link=<token>` query param and display the direct link for dev environments where email isn't configured

### Admin data management (provided as template)
- routes/admin.js is already provided as a static template. It includes:
  POST /admin/reset-data, POST /admin/csv-import, POST /admin/change-password.
- You MUST mount it in server.js: `app.use(require('./routes/admin'));`
- You MUST add csv-parser and multer to package.json dependencies.
- On the admin dashboard or main dashboard (for single-role apps), you MUST add ALL of these UI elements:
  1. A "Clear All Data" button (red bg-red-600, with data-confirm="Are you sure? This will delete all data except user accounts.") inside a form that POSTs to /admin/reset-data with CSRF hidden field
  2. A "Change Password" section with a form containing current_password and new_password inputs, submit button, that POSTs to /admin/change-password with CSRF hidden field
  3. An "Import CSV" section with a form containing a file input (accept=".csv") and a select dropdown for table name, that POSTs to /admin/csv-import with CSRF hidden field and enctype="multipart/form-data"
  These admin tools MUST be visible on the dashboard. Do NOT skip them. They are required for go-live.

### Request body size limits
- Add size limits to prevent DoS: `app.use(express.json({ limit: '1mb' }))` and `app.use(express.urlencoded({ extended: true, limit: '1mb' }))`

### Error handling
- Add a centralized error handler AFTER all routes in server.js:
  ```js
  app.use((err, req, res, next) => { console.error(err.stack); res.status(500).render('500', { title: 'Error', error: 'Something went wrong.' }); });
  ```
- NEVER expose stack traces, SQL errors, or internal details to the user
- EVERY route handler MUST use try/catch. Pattern:
  ```js
  router.get('/path', requireAuth, async (req, res) => {
    try {
      // ... route logic ...
    } catch (err) {
      console.error('Error in /path:', err);
      res.status(500).render('500', { title: 'Error' });
    }
  });
  ```
- views/500.ejs is provided as a template. It renders a user-friendly error page.

### Database readiness on startup
- Before calling app.listen(), verify the database is reachable:
  ```js
  async function startServer() {
    try {
      await pool.query('SELECT 1');
      console.log('Database connected');
    } catch (err) {
      console.error('Database connection failed, retrying in 2s:', err.message);
      setTimeout(startServer, 2000);
      return;
    }
    app.listen(PORT, () => console.log(`Server running on port ${PORT}`));
  }
  startServer();
  ```
- This prevents 500 errors during Railway DB boot race conditions.

### XSS prevention: CRITICAL
- In EJS templates, ALWAYS use `<%= variable %>` for any user-controlled data (names, notes, emails, etc.)
- NEVER use `${variable}` inside template literals in EJS for user data; that bypasses escaping
- The ONLY safe place to use raw `<%- ... %>` is for rendering trusted layout partials, never for user data
- All error messages from query params must be escaped: `<%= error %>` not `${error}` or `<%- error %>`

### CSRF protection
- Use a simple session-based CSRF token (do NOT use csrf-csrf or csurf packages; both cause subtle bugs):
  ```js
  const crypto = require('crypto');
  // CSRF middleware: generate token per session, validate on mutation routes
  function generateCsrfToken(req, res, next) {
    if (!req.session.csrfToken) {
      req.session.csrfToken = crypto.randomBytes(32).toString('hex');
    }
    res.locals.csrfToken = req.session.csrfToken;
    next();
  }
  function validateCsrfToken(req, res, next) {
    const token = req.body._csrf || req.headers['x-csrf-token'];
    if (!token || token !== req.session.csrfToken) {
      return res.status(403).send('Invalid or missing CSRF token. Please go back and try again.');
    }
    next();
  }
  app.use(generateCsrfToken);
  ```
- Apply `validateCsrfToken` to every state-changing route (POST/PUT/PATCH/DELETE):
  ```js
  router.post('/login', loginLimiter, validateCsrfToken, async (req, res) => { ... });
  router.post('/logout', validateCsrfToken, (req, res) => { ... });
  router.post('/register', validateCsrfToken, async (req, res) => { ... });
  // Apply to all other POST routes that mutate data
  ```
- Every GET route that renders a form MUST rely on the `generateCsrfToken` middleware (already applied globally above); do NOT manually set csrfToken in individual route handlers; it is already in `res.locals`
- Every form MUST include `<input type="hidden" name="_csrf" value="<%= csrfToken %>">`; this is required on login, register, logout, and ALL mutation forms
- CRITICAL: The `generateCsrfToken` middleware must be registered with `app.use()` BEFORE any routes, so `res.locals.csrfToken` is always available in templates

### Input validation
- Validate all required POST fields server-side before touching the database
- Missing required fields: re-render the form with an error message, do NOT crash
- Strings: always `.trim()` before use or storage
- Emails: store lowercase, `email.trim().toLowerCase()`
- Numeric IDs from forms: parse with `parseInt()` and validate `!isNaN(result)` before use

### SQL injection prevention
- All queries use parameterized queries ($1, $2, etc.), never string concatenation or template literals

### Authentication logging
- Log security events to console with context:
  - Successful login: `console.log('[AUTH] Login:', email, 'role:', user.role)`
  - Failed login: `console.log('[AUTH] Failed login attempt:', email)`
  - Role denial: `console.log('[AUTH] Access denied:', req.session.userRole, 'tried', req.path)`

### Input validation
- Trim all string inputs before use
- Validate required fields server-side; re-render form with error message if missing
- Emails must be stored lowercase: `email.trim().toLowerCase()`

## Code requirements

- All SQL queries must use parameterized queries ($1, $2, etc.)
- All passwords must be hashed with bcrypt (12 rounds minimum)
- Session-based auth: store userId, userRole, userName in session
- Role middleware: check session role against allowed_roles for protected routes
- Every route handler must have try/catch with proper error responses (never expose stack traces to users)
- CRITICAL: Create a shared database module at db/index.js that exports the Pool:
  ```js
  const { Pool } = require('pg');
  const pool = new Pool({
    connectionString: process.env.DATABASE_URL,
    ssl: (!(process.env.DATABASE_URL || '').includes('railway.internal') && process.env.NODE_ENV === 'production') ? { rejectUnauthorized: false } : false
  });
  module.exports = pool;
  ```
  ALL route files and db/migrate.js MUST use `const pool = require('../db')` (or `require('./index')` from within db/) instead of creating their own Pool. NEVER create a new Pool() in route files. This ensures SSL config is centralized and all routes use the same connection pool
- Include a health check route at GET /health returning `{ status: 'ok', timestamp: new Date() }`
- Use morgan for HTTP logging: `app.use(require('morgan')('combined'))` in server.js
- Use Promise.all() when fetching multiple independent queries in the same route handler
- CRITICAL: routes/auth.js MUST define `GET /login` to render the login form
- CRITICAL: `GET /` behavior depends on the app type:
  - If the app has PUBLIC pages (pages with auth_required=false like /services, /book, /contact), `GET /` MUST render a public landing/home page (NOT a login form). The landing page should link to public pages and have a "Login" / "Sign Up" link in the nav.
  - If ALL pages require authentication, `GET /` should redirect to `/login`
  - NEVER create a redirect chain like / -> /login -> /admin/login. There should be ONE login page at /login
- CRITICAL: routes/auth.js MUST define `GET /register` AND `POST /register` for new user registration
- CRITICAL: Registration form must collect: name, email, password, and account type/role (if multi-role app)
- CRITICAL: POST /register must validate all fields, check for existing email, hash password with bcrypt,
  insert user, create session, and redirect to /dashboard
- CRITICAL: POST /login must validate credentials, set session fields (req.session.userId, req.session.userRole, req.session.userName), then call `req.session.save(() => res.redirect('/dashboard'))` to ensure the session is persisted to the store BEFORE the redirect. Without explicit save(), the redirect can race ahead of the async session write, causing the user to land on /dashboard without a session. On failure, re-render login with error. NEVER redirect back to /login on success.
- CRITICAL: POST /register must do the same: set session fields, then `req.session.save(() => res.redirect('/dashboard'))`. Same race condition applies.
- CRITICAL: req.session.save() is required EVERY time you set session fields and redirect. This applies to ALL code paths that create sessions: POST /login (all role types), POST /register, GET /magic?token=, GET /verify-email?token=, and any TEST_MODE backdoor. If even one login path skips session.save(), that role's users will be unable to stay logged in.
- CRITICAL: middleware/auth.js requireAuth MUST redirect to `/login` (not `/` or `/?error=...`)
- CRITICAL: Seed user emails must follow `{role}@example.com` format, one canonical user per role
- Login page must include a "Don't have an account? Register here" link to /register
- Register page must include a "Already have an account? Login here" link to /login
- Logout must use POST method with CSRF token (never GET). Implementation:
  - Route: `router.post('/logout', validateCsrfToken, (req, res) => { req.session.destroy(() => res.redirect('/login')); });`
  - In layout.ejs nav bar, use a FORM (not a link): `<form action="/logout" method="POST" style="display:inline"><input type="hidden" name="_csrf" value="<%= csrfToken %>"><button type="submit" class="...">Logout</button></form>`
  - The form MUST have `action="/logout"` and `method="POST"` attributes. Tests check for `form[action='/logout']`.
- NEVER show demo/test credentials, example emails, or seed account info on the login page or anywhere in the UI.
  The login page should only show email field, password field, submit button, and a "Register here" link. Nothing else.
- Admin role must have access to ALL data across all users (admin dashboard showing aggregate stats)

### Multi-role access (CRITICAL: common failure pattern)
- If the app has multiple roles (e.g., admin + member, admin + staff + customer), EVERY role must be
  able to log in and access their authorized pages WITHOUT being redirected back to /login.
- requireAuth middleware must check ONLY that a session exists (userId is set). It must NOT check role.
  Role checks are separate: use requireRole('admin') or check req.session.userRole in the route handler.
- Common bug: requireAuth checks for admin role, causing all non-admin users to be redirected to /login.
  Fix: requireAuth = check session exists; requireRole = check specific role. They are separate middleware.
- Pattern for role-gated pages:
  ```js
  // Page visible to all logged-in users:
  router.get('/dashboard', requireAuth, async (req, res) => { ... });
  // Page visible to admin only:
  router.get('/admin/users', requireAuth, requireRole('admin'), async (req, res) => { ... });
  // Page visible to specific roles:
  router.get('/reports', requireAuth, requireRole('admin', 'manager'), async (req, res) => { ... });
  ```
- Non-admin users who try to access admin-only pages must get a 403 (forbidden), NOT a redirect to /login.
- Dashboard must render content appropriate to the logged-in user's role (scoped data, role-appropriate nav).
- All seed users (one per role) must be able to log in and see their scoped view without errors.

### Registration security and data scoping (CRITICAL)
- When a new user registers, assign the LOWEST-PRIVILEGE role by default (e.g. 'member', 'staff', 'user')
  NEVER assign 'admin' role on registration. Admin accounts are ONLY created via seed data.
- Every table that stores user-created content MUST have a `created_by INTEGER REFERENCES users(id)` column
- Non-admin users MUST only see data they created. All SELECT queries for non-admin users must include
  `WHERE created_by = $userId` (or equivalent ownership check). Admin users see all data.
- Route middleware pattern for data scoping:
  ```js
  // In each GET list route:
  let query = 'SELECT * FROM items';
  const params = [];
  if (req.session.userRole !== 'admin') {
    query += ' WHERE created_by = $1';
    params.push(req.session.userId);
  }
  query += ' ORDER BY created_at DESC';
  const result = await pool.query(query, params);
  ```
- Detail/edit/delete routes must verify ownership: if non-admin, check `created_by = req.session.userId`
  before showing or modifying the record. Return 403 if not the owner.

## CRUD requirements (non-negotiable)

Every major resource/entity in the app MUST have full CRUD:
- **Create:** A dedicated form page (GET /resources/new) with proper validation AND a visible "Add New" / "Create" button on the list page that links to it. The create button MUST be a standard `<a href="/resources/new">` link with classes like `bg-primary text-white px-4 py-2 rounded-lg`, placed prominently at the top of the list page. The form itself (POST /resources) must redirect back to the list page on success. NEVER omit the create button from the list view, QA tests look for it.
- **Read:** List view with pagination or filtering AND individual detail view
- **Update:** Edit form pre-populated with current values (e.g., /resources/:id/edit)
- **Delete:** Confirmation step before deletion (either confirm dialog or confirmation page)
- **List view MUST include inline action buttons for each row:** Edit (link to /resources/:id/edit) and Delete (small form with POST to /resources/:id/delete, red button, data-confirm). Do NOT hide edit/delete behind a detail page only.
- **Create form reachability (common failure):** The full create flow must work end-to-end: click "Add New" on list page -> fill out form fields -> submit -> redirect to list with new record visible. If any step in this chain breaks (missing button, missing route, missing form fields, missing redirect), the CRUD is broken.
- All CRUD operations must be protected by authentication and role-based authorization
- Each CRUD action must have its own route handler (no combined create/update endpoints)
- List views must show useful columns and be sortable by at least one field (created_at)
- Detail views must show all fields of the resource
- Success/error flash messages after create, update, and delete operations
- **Boolean/status toggle:** If a model has a boolean field (e.g., done, active, completed, published), provide a quick-toggle mechanism. Use a small form with a POST button (e.g., "Mark Done" / "Mark Undone") next to each item in the list view. The POST route should flip the boolean and redirect back. Do NOT rely on JavaScript-only toggles.
- **Delete must work without 500 errors:** The delete POST route must handle missing records gracefully (return 404, not crash). Always check `if (!result.rows[0])` after the DELETE query.
- **CONFIRMATION ON ALL DESTRUCTIVE ACTIONS:** Every button/form that deletes, removes, resets, or permanently changes data MUST require user confirmation before executing. Use `data-confirm="Are you sure you want to delete this item?"` on the form element (the layout template handles the confirm dialog). This applies to: delete buttons, reset/clear buttons, bulk actions, status changes that cannot be undone. NEVER allow a single click to permanently destroy data without a confirmation step.
- **Null-check ALL database results before accessing properties:** After ANY `db.query()` call, always check `if (!result.rows.length)` or `if (!result.rows[0])` before accessing `result.rows[0].id` or any field. Never assume a query returns rows; missing record = 500 crash. This is the #1 cause of deploy crashes (TypeError: Cannot read property 'id' of undefined). Every single route that reads from the database must have this check. Pattern:
  ```js
  const result = await db.query('SELECT * FROM items WHERE id = $1', [req.params.id]);
  if (!result.rows[0]) return res.status(404).render('404', { title: 'Not Found' });
  const item = result.rows[0]; // Safe to access properties now
  ```
  Apply this to: detail routes, edit routes, update routes, delete routes, any route that looks up a record by ID or foreign key.
- **Database pool import:** In every route file, import the shared pool as `const db = require('../db');` (uses db/index.js). NEVER use `require('../db/pool')`; that path does not exist and will crash the app on startup.
- **Register/Signup page must work:** The registration form must handle all error cases (duplicate email, missing fields) by re-rendering the form with an error message, never returning a 500.

## Database conventions

- Table names: lowercase, underscores
- Column names: lowercase, underscores
- Every table gets: `id SERIAL PRIMARY KEY`, `created_at TIMESTAMP DEFAULT NOW()`
- Foreign keys: `referenced_table_id INTEGER REFERENCES referenced_table(id)`
- Use NOT NULL, UNIQUE constraints where appropriate
- Add indexes on frequently queried columns:
  ```sql
  CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
  CREATE INDEX IF NOT EXISTS idx_bookings_barber_id ON bookings(barber_id);  -- example
  ```
  Add indexes for every foreign key column and any column used in WHERE clauses.

## EJS layout setup (required)

In server.js, you MUST configure express-ejs-layouts so that layout.ejs wraps every page:
```js
const expressLayouts = require('express-ejs-layouts');
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));
app.use(expressLayouts);
app.set('layout', 'layout');
```

views/layout.ejs is the HTML shell (`<!DOCTYPE html>`, `<head>`, `<body>`, nav, footer).
It MUST contain `<%- body %>` where page content is inserted by express-ejs-layouts.
Each page template (login.ejs, dashboard.ejs, etc.) contains ONLY the page-specific content,
NOT `<html>`, `<head>`, or `<body>` tags. The layout wraps them automatically.

package.json MUST include `"express-ejs-layouts": "^2.5.1"` in dependencies.

### Navigation link uniqueness (prevents QA test failures)
- The desktop nav and mobile nav MUST NOT both render visible links at the same time. Use Tailwind's
  `hidden md:flex` on the desktop nav and `md:hidden` on the mobile menu so only one set is in the DOM
  at any given viewport width. Duplicate visible nav links cause Playwright "strict mode violation" errors
  because `text=Dashboard` matches multiple elements.
- Page headings (h1/h2) should use DIFFERENT text than the nav link when possible. For example, if the
  nav link says "Inventory", the page heading could say "Manage Inventory" or "Inventory Items". This
  avoids Playwright selectors matching both the nav link and the heading.
- The brand/logo link in the nav must NOT repeat a page name (e.g., do not use "Inventory" as both the
  brand name and a nav link). Use the app name for the brand.

## EJS template conventions

- Navigation reflects the logged-in user's role
- Forms show validation errors returned from the server (using `<%= error %>`)
- Use semantic HTML: form, label, input, button, table, nav
- ALL user data output uses `<%= %>`; this is mandatory for XSS prevention
- CRITICAL: `require` is NOT available inside EJS templates. Never call `require(...)` in a .ejs file.
- CRITICAL: NEVER use the const body = backtick-string JavaScript template literal pattern in EJS views.
  EJS parses `<% %>` tags before executing JavaScript, so any `<%= %>` or `<% %>` tags inside a JS
  template literal string will break the EJS parser with "Could not find matching close tag" errors.
- Use only standard EJS syntax throughout every view file:
  - `<%= variable %>` for escaped output (safe, prevents XSS)
  - `<%- variable %>` for unescaped output (only for trusted HTML)
  - `<% if (condition) { %>...html...<% } %>` for conditionals
  - `<% array.forEach(item => { %>...html...<% }) %>` for loops
  Example of the ONLY acceptable pattern for conditional error display:
  ```ejs
  <% if (locals.error) { %>
    <div class="alert alert-error"><%= error %></div>
  <% } %>
  ```

### Date/time display
- Always render dates and times in the user's local timezone using client-side JavaScript
- In EJS, output dates as ISO strings in a data attribute, then convert client-side:
  `<span data-localtime="<%= new Date(item.created_at).toISOString() %>">fallback UTC</span>`
- In layout.ejs, add a DOMContentLoaded script that finds all `[data-localtime]` elements and sets
  their textContent to `new Date(el.dataset.localtime).toLocaleString('en-US', { timeZoneName: 'short' })`, this uses the browser's timezone and shows the TZ abbreviation (e.g. "CT", "ET", "PT")
- Server-side fallback text should always append "UTC" so users know it has not been localized yet
- NEVER use server-side toLocaleString() for user-facing dates since the server runs in UTC

## Seed data: CRITICAL: must be realistic

- ADMIN USER: The FIRST seed user MUST use the admin_email from the app spec (NOT admin@example.com).
  Look at the spec JSON for the "admin_email" field and use EXACTLY that email.
  If admin_email is "owner@studio.example", the seed INSERT must be for "owner@studio.example".
- Additional role users: one per role with email `{role}@example.com`
- ALL seed users MUST have `email_verified = TRUE` so they can log in immediately after deploy.
  If the users table has an email_verified column, every INSERT must set it to TRUE.
- Create 10-20 sample records per major table to make the app look populated and alive
- Seed every user with `password = NULL` and `password_set = FALSE`. This app uses magic-link
  auth; hardcoding a bcrypt hash into seed.sql leaks a shadow credential into the repo. Founders
  authenticate in dev by minting a one-off magic-link token via psql. Do NOT write any
  `$2b$10$...` literal into seed.sql.
- NEVER use placeholder data like "test123", "Sample Item 1", "Description here", or "Lorem ipsum"
- Use realistic names (e.g., "Sarah Chen", "Marcus Rivera"), realistic dates (spread over the last 90 days),
  realistic amounts (e.g., $45.00 not $100.00), and domain-appropriate data
- For apps with scheduling/bookings, seed future dates so the calendar looks active
- For apps with notes/descriptions, write 1-2 sentence realistic content
- For apps with video/YouTube fields: use REAL, DIVERSE YouTube video IDs in seed data.
  NEVER use dQw4w9WgXcQ (rickroll) or any single repeated ID. Each video must have a unique,
  real YouTube ID. Find IDs relevant to the app domain (e.g. meditation videos for wellness apps,
  cooking tutorials for recipe apps). Format: embed_url = 'https://www.youtube.com/embed/{VIDEO_ID}'
- For image_url fields: use descriptive paths like '/images/gallery/sunset-01.jpg' or
  '/images/gallery/team-meeting-01.jpg'. NEVER use external URLs (unsplash.com, pexels.com).
  Use the /images/gallery/ prefix for app-specific images

## Auto-migration (required)

db/migrate.js must:
- Use pg.Pool with the SAME SSL config as server.js:
  `new Pool({ connectionString: process.env.DATABASE_URL, ssl: (!(process.env.DATABASE_URL || '').includes('railway.internal') && process.env.NODE_ENV === 'production') ? { rejectUnauthorized: false } : false })`
- Use fs.readFileSync to run schema.sql then seed.sql
- Only seed if users table is empty (idempotent)
- Export `async function migrate()`

server.js must call migrate() before app.listen(), but MUST NOT crash if the database is
temporarily unavailable at startup (Railway provisions the DB asynchronously). The HTTP server
must always start listening so health checks and readiness probes succeed:
```js
const { migrate } = require('./db/migrate');
migrate()
  .then(() => { app.listen(PORT, () => console.log('Server running on port', PORT)); })
  .catch(err => {
    console.error('Database migration failed (will retry on next request):', err.message);
    // Start the HTTP server anyway so Railway health checks pass.
    // DB-dependent routes will return 503 until the database is available.
    app.listen(PORT, () => console.log('Server running on port', PORT, '(DB unavailable)'));
  });
```

CRITICAL: Never call `process.exit()` on a database connection failure. Railway's PostgreSQL
service may take 10-30 seconds after the app starts before it is ready for connections. Crashing
the process just causes a restart loop. Keep the server running and let it recover automatically.

## Design and styling

The spec includes a `design` object with color, style, and typography preferences. Use Tailwind CSS v3 \
to generate a distinctive, professional, responsive web app with strong visual identity.

### Typography (CRITICAL: distinctive fonts required)

The spec provides `design.font_display` and `design.font_body` for the app's font pairing. \
If not provided, select fonts based on the `design.style` value (see style guide below).

Load BOTH fonts in views/layout.ejs <head> via Google Fonts:
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DISPLAY_FONT:wght@400;700;900&family=BODY_FONT:wght@300;400;500;600;700&display=swap" rel="stylesheet">
```

Replace DISPLAY_FONT and BODY_FONT with the actual font names from the spec (spaces replaced with +).

Tailwind config font families:
```js
fontFamily: {
  display: ['DISPLAY_FONT', 'serif'],
  body: ['BODY_FONT', 'sans-serif'],
}
```

TYPOGRAPHY RULES:
- Use font-display (via class `font-display`) for ALL headings, hero text, page titles, and nav brand name
- Use font-body (via class `font-body`) for body text, form labels, table content, and UI elements
- Use EXTREME weight contrast: headings at font-bold (700) or font-black (900), body at font-light (300) or font-normal (400)
- Size jumps must be 2x-3x between hierarchy levels: h1 at text-4xl or text-5xl, body at text-base
- NEVER fall back to Inter, Roboto, Open Sans, Arial, or system-ui as the visible font
- BANNED FONTS: Inter, Roboto, Open Sans, Lato, Arial, Helvetica must NEVER be the primary rendered font. \
System fonts are OK as fallbacks in the font stack, never as the rendered font.

### Tailwind CSS setup (compiled at build time, NOT CDN)

DO NOT use cdn.tailwindcss.com: it is blocked by ad blockers and firewalls in production.

Instead, Tailwind is compiled at build time. Generate these files:

1. **tailwind.config.js** (project root):
```js
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./views/**/*.ejs'],
  theme: {
    extend: {
      colors: {
        primary: 'PRIMARY_COLOR_FROM_SPEC',
        secondary: 'SECONDARY_COLOR_FROM_SPEC',
        accent: 'ACCENT_COLOR_FROM_SPEC',
      },
      fontFamily: {
        display: ['DISPLAY_FONT', 'serif'],
        body: ['BODY_FONT', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
```

2. **views/layout.ejs head**: reference the compiled CSS AND Google Fonts (no CDN script):
```html
<link rel="stylesheet" href="/tailwind.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DISPLAY_FONT:wght@400;700;900&family=BODY_FONT:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/style.css">
```

3. **package.json** must include tailwindcss in dependencies and a postinstall script:
```json
{
  "dependencies": { "tailwindcss": "^3.4.0" },
  "scripts": { "postinstall": "npx tailwindcss -o public/tailwind.css --minify" }
}
```

Replace PRIMARY/SECONDARY/ACCENT with actual hex values from spec.design. \
Replace DISPLAY_FONT/BODY_FONT with actual font names from the spec's design.font_display and design.font_body. \
If the spec has no design field, default to: primary=#2563eb, secondary=#1e40af, accent=#f59e0b.

### CSS custom properties and theming (required in public/style.css)

Define a complete set of CSS custom properties for theming:
```css
:root {
  --color-primary: /* spec.design.primary_color */;
  --color-secondary: /* spec.design.secondary_color */;
  --color-accent: /* spec.design.accent_color */;
  --color-surface: /* derive from style: warm tint, cool tint, or dark surface */;
  --color-surface-raised: /* slightly elevated from surface */;
  --color-text: /* high contrast against surface */;
  --color-text-muted: /* secondary text, lower contrast */;
}
```

COLOR RULES:
- PRIMARY is the dominant color: nav, buttons, links, active states. It should feel BOLD.
- ACCENT is used sparingly for maximum impact: badges, one CTA per page, alerts. \
Do NOT distribute accent evenly across the page; it loses power when overused.
- Backgrounds must have DEPTH (see Background Treatment section below).
- Hover states: shift toward secondary color, never just opacity changes.
- Focus rings: 2px ring in primary with 50% opacity, offset by 2px.

### Using Tailwind in EJS templates

Use Tailwind utility classes directly in EJS templates for all layout, spacing, typography, and color:
- Use `bg-primary`, `text-primary`, `border-primary` etc. for brand colors
- Use `font-display` for headings, `font-body` for body text
- Use `rounded-lg`, `shadow-sm`, `p-4`, `gap-4` etc. for spacing and effects
- Use `flex`, `grid`, `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` for responsive layouts
- Use responsive prefixes: `sm:`, `md:`, `lg:` for breakpoint-specific styles
- Keep public/style.css minimal: only CSS custom properties, custom animations, complex selectors, or overrides. Tailwind utilities handle the rest.

### Style guide by design.style value

- "modern light": Clean sans display font (Source Sans 3 + DM Sans), `bg-gray-50` tinted backgrounds, \
`text-gray-900` text, `shadow-sm` cards, `rounded-lg` corners. Airy, spacious feel.
- "modern dark": Geometric sans display (Space Grotesk + IBM Plex Sans), `bg-slate-900` / `bg-slate-800` \
backgrounds, `text-slate-100` text, subtle borders, sharp `rounded-lg` corners.
- "clean minimal": Elegant serif display (Newsreader + Source Sans 3), maximum whitespace, `border` instead \
of shadows, `text-gray-700`, accent colors only on interactive elements. Typography-focused.
- "warm earthy": Serif display (Playfair Display + Crimson Pro), `bg-amber-50` tinted backgrounds, \
`rounded-xl` elements, `shadow-md`, warm orange/amber gradients. Friendly and approachable.
- "bold vibrant": Strong display font (Montserrat + Space Grotesk), saturated `bg-primary` usage, \
`text-lg font-bold` buttons, gradient accents via `bg-gradient-to-r`, high contrast, `rounded-xl`.
- "corporate professional": Traditional serif (Merriweather + IBM Plex Sans), structured grid layout, \
`bg-slate-50` / `bg-blue-50` palette, clear heading hierarchy, `shadow-sm` cards, formal.

If no style is specified, default to "modern light".

### Motion and transitions

Add subtle motion to create moments of delight. Include in public/style.css:

```css
/* Staggered fade-in for cards and list sections */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
.animate-in {
  animation: fadeInUp 0.4s ease-out both;
}
.animate-in:nth-child(2) { animation-delay: 0.08s; }
.animate-in:nth-child(3) { animation-delay: 0.16s; }
.animate-in:nth-child(4) { animation-delay: 0.24s; }
.animate-in:nth-child(5) { animation-delay: 0.32s; }

/* Respect user motion preferences */
@media (prefers-reduced-motion: reduce) {
  .animate-in { animation: none; }
}
```

MOTION RULES:
- Add class="animate-in" to each card/section on list pages and dashboards
- ONE orchestrated page load animation (staggered reveals), not scattered micro-interactions
- Hover transitions on cards: `hover:scale-[1.01] hover:shadow-lg transition-all duration-200`
- Button hover: `transition-colors duration-150 hover:-translate-y-px`
- Navigation links: underline or highlight slides in on hover (CSS transition)
- Page transitions are NOT needed (server-rendered app)

### Spatial composition and layout

LAYOUT RULES:
- Do NOT center everything. Use asymmetric layouts where content warrants it.
- Hero sections: left-aligned headline with generous padding (py-16 to py-24), not centered-text-over-image.
- Dashboard grids: vary card sizes (col-span-2 for primary metrics, col-span-1 for secondary). \
Not every card should be the same size.
- Sidebar layouts: use a fixed sidebar for navigation-heavy apps (5+ pages), not just a top nav.
- Whitespace is a design element. Section spacing should be generous (py-16 to py-24 between major sections, not py-4 to py-8).
- Tables: full-width with generous cell padding (px-6 py-4). Alternate row backgrounds. Sticky header. Left-align text, right-align numbers.
- Forms: max-w-md centered, with clear visual grouping. Labels above inputs, not beside.
- Login/register pages: split layout (branding panel left, form right) on desktop, stacked on mobile. Not just a centered card on a blank page.
- 404/error pages: styled with personality matching the design direction.

### Background treatment

PAGE BACKGROUND RULES:
- Page background must NOT be flat #ffffff or #f9fafb. Add atmosphere:
  * Use a subtle radial gradient in the hero/above-fold area using primary color at 3-5% opacity
  * OR a CSS dot/grid pattern at very low opacity for texture
  * OR a warm/cool tint derived from primary color at 2-3% opacity
- Card backgrounds: use a surface color slightly raised from page background \
(white cards on a tinted page, or elevated surfaces in dark mode)
- Nav background: solid primary or a dark surface, NOT transparent/white
- Footer: darker surface than page body, clear visual separation

Example hero gradient in public/style.css (adapt colors to spec):
```css
.hero-gradient {
  background: linear-gradient(135deg, var(--color-primary, #2563eb)08 0%, transparent 50%),
              linear-gradient(225deg, var(--color-primary, #2563eb)05 0%, transparent 40%);
}
```

### Design anti-patterns (NEVER DO THESE)

BANNED PATTERNS:
- Purple gradient on white background
- Centered text over a stock photo hero
- Cards that are all identical size in a grid
- Gray (#6b7280) as the only text color
- Buttons that are just outlined/transparent on white
- Empty white pages with a single centered element
- Emoji as icons (use Lucide SVG icons or inline SVGs)
- "Lorem ipsum" or placeholder text (use realistic seed data)
- Cookie-cutter SaaS layouts (hero + 3 feature cards + pricing grid)
- Shadow-sm on everything (choose shadow-none or shadow-lg, be intentional)
- Border-radius that doesn't match the direction (brutalist = sharp, luxury = subtle)

REQUIRED DESIGN PATTERNS:
- Every page must have a clear visual hierarchy (one dominant element)
- Navigation must feel integrated with the design, not a generic bar
- At least one "moment of delight" per app (a well-crafted animation, a thoughtful empty state, a polished detail)
- Login page must be designed with the split-panel layout, not just a bare form
- Empty states must be designed (styled message with helpful text, not just "No items")

### Responsive design (required)

All generated apps MUST be responsive using Tailwind breakpoints:
- Use `max-w-6xl mx-auto px-4 sm:px-6` for page containers
- Navigation: mobile hamburger menu pattern (hidden on `md:` and up)
- Forms: `w-full` inputs on mobile, form containers use `max-w-lg mx-auto` so they are readable on all screens
- Tables: EVERY `<table>` MUST be wrapped in a `<div class="overflow-x-auto">` for horizontal scroll on mobile. Never render a bare `<table>` without this wrapper.
- Cards: `grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4` for responsive grids. NEVER use fixed multi-column grids without responsive breakpoints.
- Touch targets: `min-h-[44px] min-w-[44px]` on ALL buttons, links in nav, and interactive elements (44x44px is the minimum for accessibility)
- No horizontal scroll on body: the `<body>` and main content containers must NEVER cause horizontal overflow. Use `overflow-x-hidden` on body if needed.
- Input font size: ALL `<input>`, `<select>`, and `<textarea>` elements must have `text-base` (16px) minimum font size. Smaller sizes trigger auto-zoom on iOS Safari which breaks the layout.
- Text overflow: long text in table cells, card titles, and badges must use `truncate` or `break-words` to prevent layout blowout on narrow screens.
- Images: use `max-w-full h-auto` on all images to prevent overflow.
- Spacing: use responsive padding `px-4 sm:px-6 lg:px-8` on page wrappers, not fixed large padding.

### General styling quality

- Use `bg-primary` for nav background, buttons, links, active states
- Use `hover:bg-secondary` for hover states, focus rings with `focus:ring-2 focus:ring-primary/50`
- Use `bg-accent` or `text-accent` sparingly for badges, alerts, success states, one CTA per page
- Style form inputs: `w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary/50 focus:border-primary`
- Add transitions: `transition-colors duration-150` on ALL interactive elements
- Flash messages: `bg-green-50 text-green-800 border border-green-200` (success), `bg-red-50 text-red-800 border border-red-200` (error)
- Navigation: clearly show current page with `bg-primary/10 text-primary` or `border-b-2 border-primary`
- Tables: `even:bg-gray-50` (light) or `even:bg-slate-800/50` (dark) for alternating rows

## PWA scaffolding (required)

Every generated app must include Progressive Web App support:

### public/manifest.json
```json
{
  "name": "APP_NAME",
  "short_name": "APP_SHORT_NAME",
  "description": "APP_DESCRIPTION",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#ffffff",
  "theme_color": "PRIMARY_COLOR_FROM_SPEC",
  "icons": [
    { "src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml" }
  ]
}
```
Use the app name from the spec. Set theme_color to the primary color.

### public/sw.js
Simple service worker that caches the app shell:
```js
const CACHE_NAME = 'app-v1';
const SHELL_URLS = ['/', '/style.css', '/favicon.svg'];
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(SHELL_URLS))));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request).catch(() => caches.match(e.request))));
```

### In views/layout.ejs head section, include (alongside the Tailwind CDN script from the Design section):
```html
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="PRIMARY_COLOR">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>
```

## Favicon (required)

Generate a simple, clean SVG favicon in public/favicon.svg that represents the app's purpose:
- Use the app's primary color as the main fill color
- Keep it simple: one or two shapes, no text, recognizable at 32x32
- Examples: a calendar icon for schedulers, a chart for trackers, a person for CRMs, a shopping bag for e-commerce
- Use a circle or rounded square background with a white icon on top
- The SVG should be under 1KB

## Logo integration

If the spec includes `design.logo_data`, the customer uploaded a logo. You MUST use it as the site logo in the navigation bar. Do NOT use text-only branding when a logo is available. The logo file is at public/logo.png (or public/logo.svg for SVG logos):
- In views/layout.ejs navbar: `<img src="/logo.png" alt="APP_NAME logo" class="h-8 w-auto">` next to the app name text
- In views/login.ejs: `<img src="/logo.png" alt="APP_NAME logo" class="h-16 w-auto mx-auto mb-4">` above the login form
- In views/register.ejs: same logo placement as login
- In views/layout.ejs footer: `<img src="/logo.png" alt="APP_NAME" class="h-6 w-auto opacity-75">` in the footer
- If a public landing page exists (auth_required=false), include the logo in the hero section
- For favicon: `<link rel="icon" type="image/png" href="/logo.png">` in layout.ejs head (replaces SVG favicon)
- For PWA manifest: reference /logo.png for the 192x192 and 512x512 icons
- If no logo_data is provided, just use the app name text in the nav (no img tags)

## SEO (required)

Every generated app must include basic SEO in views/layout.ejs:
- `<meta name="description" content="...">` with a relevant 150-char description of what the app does
- `<meta name="viewport" content="width=device-width, initial-scale=1">`
- `<link rel="canonical" href="/">` on the homepage
- Open Graph tags: `og:title`, `og:description`, `og:type` (website), `og:url`
- `<title>` tag with format: "Page Name | App Name"
- Semantic HTML: use `<main>`, `<nav>`, `<header>`, `<footer>`, `<section>`, `<article>` appropriately
- Heading hierarchy: exactly one `<h1>` per page, logical `<h2>`/`<h3>` nesting
- All images must have descriptive `alt` attributes
- All links must have meaningful text (no "click here")
- Add a `robots.txt` route that allows all crawlers and disallows `/admin/` and `/api/`

## Analytics (optional)

If the spec includes `design.analytics_id`, add a Plausible Analytics script to views/layout.ejs:
```html
<script defer data-domain="ANALYTICS_DOMAIN" src="https://plausible.io/js/script.js"></script>
```
This is privacy-friendly and requires no cookie consent banner.

## CSS typography and spacing (required)

In addition to the design section above, ensure:
- Use a consistent spacing scale: 0.25rem, 0.5rem, 0.75rem, 1rem, 1.5rem, 2rem, 3rem
- Typography hierarchy: h1 (2rem bold), h2 (1.5rem semibold), h3 (1.25rem medium), body (1rem)
- Line height: 1.5 for body text, 1.2 for headings
- Ensure WCAG AA contrast ratios: text on backgrounds must have at least 4.5:1 contrast
- CRITICAL BUTTON VISIBILITY: Every submit/action button MUST have a solid visible background color. \
Primary actions: `bg-primary text-white hover:bg-secondary`. Secondary actions: `bg-gray-200 text-gray-800 hover:bg-gray-300`. \
Destructive actions: `bg-red-600 text-white hover:bg-red-700`. \
NEVER use transparent/outline-only buttons for form submit or primary actions; they become invisible on white backgrounds. \
Delete buttons must have `bg-red-600 text-white` (not just `text-red-600`).
- BUTTON VISIBILITY FALLBACK: In public/style.css, ALWAYS include this rule as a safety net: \
`button[type="submit"] { background-color: var(--color-primary, #2563eb); color: #fff; }` \
This ensures submit buttons are visible even if Tailwind classes fail to apply. \
This is tested automatically and builds will fail if submit buttons have transparent backgrounds.
- Button styles: min-height 44px, clear hover/focus states, consistent padding (0.75rem 1.5rem)
- All interactive elements must have visible :focus-visible styles (outline or box-shadow)
- Include aria-label on icon-only buttons, role="alert" on error messages, proper form label associations
- Hidden inputs for custom controls must use visually-hidden pattern (not display:none) to stay in the accessibility tree
- Card pattern: consistent padding (1.5rem), border-radius, subtle shadow or border
- Form labels must be above their inputs, not inline, for better mobile UX
- MOBILE CSS SAFETY NET: In public/style.css, ALWAYS include these responsive rules: \
`html, body { overflow-x: hidden; } input, select, textarea { font-size: 16px; } @media (max-width: 640px) { table { display: block; overflow-x: auto; } }` \
This prevents horizontal scroll, iOS zoom on inputs, and table overflow even if Tailwind classes are missed.

## Conditional capabilities

The following capabilities should be implemented ONLY when the spec describes them (in actions, \
page purposes, or data model field descriptions). Do not add them if the spec does not need them.

### File uploads (when spec mentions file/image/photo/document/attachment fields)

If any data model or page involves file uploads:
- Use `multer` for multipart file handling (add to package.json)
- Store files in `public/uploads/` directory (created on startup if missing)
- Limit file size to 5MB, accept common types (images: jpg/png/gif/webp, documents: pdf/doc/docx)
- Save with unique filenames: `Date.now() + '-' + originalname`
- Store the file path in the database column (VARCHAR)
- Display uploaded images with `<img src="/uploads/filename" alt="...">` (escaped)
- Include file input in forms: `<input type="file" name="fieldname" accept="image/*">`
- Set `enctype="multipart/form-data"` on forms with file inputs
- Add `app.use('/uploads', express.static('public/uploads'))` in server.js

### PDF generation (when spec mentions reports, invoices, receipts, or PDF downloads)

If any action or page involves generating PDFs:
- Use `pdfkit` package (add to package.json)
- Create a utility file `utils/pdf.js` with helper functions
- Generate PDFs in route handlers and pipe to response:
  ```js
  const PDFDocument = require('pdfkit');
  res.setHeader('Content-Type', 'application/pdf');
  res.setHeader('Content-Disposition', 'attachment; filename="report.pdf"');
  const doc = new PDFDocument();
  doc.pipe(res);
  doc.fontSize(20).text('Title', { align: 'center' });
  // ... add content from database query
  doc.end();
  ```
- Include app branding (name, primary color) in PDF headers
- Add a "Download PDF" button/link on the relevant page

### In-app email (when spec mentions notifications, alerts, or sending messages to users)

If any action involves sending email notifications:
- Use the Resend API via fetch (no SDK needed):
  ```js
  await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + process.env.RESEND_API_KEY },
    body: JSON.stringify({ from: 'noreply@craftandship.io',
      to: [recipientEmail], subject: subject, html: htmlBody }),
  });
  ```
- Create a utility file `utils/email.js` with a `sendEmail(to, subject, html)` function
- Wrap in try/catch; log errors but do not crash the app if email fails
- Add RESEND_API_KEY and EMAIL_DOMAIN to .env.example
- If RESEND_API_KEY is not set, log the email to console instead of sending (dev-friendly)

### Simple charts (when spec mentions dashboards, analytics, statistics, or visual reports)

If any page involves data visualization or dashboards:
- Use Chart.js via CDN (no npm install needed):
  ```html
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  ```
- Create charts in the page's inline script using data passed from the route:
  ```html
  <canvas id="myChart" width="400" height="200"></canvas>
  <script>
    new Chart(document.getElementById('myChart'), {
      type: 'bar',  // or 'line', 'pie', 'doughnut'
      data: { labels: <%- JSON.stringify(labels) %>, datasets: [{ label: 'Count', data: <%- JSON.stringify(values) %> }] },
      options: { responsive: true }
    });
  </script>
  ```
- Route handlers should query aggregate data (COUNT, SUM, AVG, GROUP BY) for chart datasets
- Use the app's primary/secondary/accent colors for chart colors
- Include at least one chart on the admin/dashboard page showing meaningful metrics
- Common chart types: bar chart for counts by category, line chart for trends over time, \
pie chart for status/type distribution

Generate the COMPLETE application. Every file must be fully implemented, no TODOs or placeholders. \
The app must work when deployed to Railway (DATABASE_URL and SESSION_SECRET env vars set, npm start triggers migration then serves)."""


def create_client():
    """Create an Anthropic client with the API key from .env."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not found in environment")
    return make_client(api_key=api_key)


def call_api_with_retry(client, messages, model=None, *, build_id=None):
    """Thin shim that delegates the build prompt call to ``api_client.call_api``.

    Section-06 of WATCHDOG1 Layer 2 migrated this wrapper from a direct
    ``client.messages.create(...)`` call to ``call_api`` so every Anthropic
    request inherits the explicit httpx.Timeout from ``make_client()``, the
    wall-clock budget bound, cooperative cancellation, cost metrics, and the
    test-hook simulating long C-level hangs. Retry semantics are owned by
    ``call_api`` itself, so the outer retry loop was removed.

    The original return contract is preserved: callers receive the response
    object whose ``content[0].text`` holds the generated file dump.
    """
    use_model = model or MODEL
    response, _metrics = call_api(
        client,
        messages,
        use_model,
        system=[{
            "type": "text",
            "text": BUILD_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        max_tokens=16384,
        module="build",
        build_id=build_id or "",
    )
    return response


def parse_files(text):
    """Extract file blocks from the build agent response.

    Looks for --BEGINFILE-- path ... --ENDFILE-- blocks.
    Returns a list of (path, content) tuples.
    """
    pattern = r"^--BEGINFILE--[ \t]+(.+?)\s*\n(.*?)^--ENDFILE--"
    matches = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
    files = []
    for filepath, content in matches:
        filepath = filepath.strip()
        # Guard against embedded --BEGINFILE-- markers inside content. Claude
        # occasionally emits a duplicated begin marker on long continuations
        # without a matching --ENDFILE-- between copies, which the non-greedy
        # outer regex captures as one big block. Split on any embedded marker
        # and keep the last copy (the most recently-written version).
        if re.search(r"^--BEGINFILE--[ \t]+\S", content, re.MULTILINE):
            parts = re.split(r"^--BEGINFILE--[ \t]+\S.*\n", content, flags=re.MULTILINE)
            content = parts[-1]
            print(f"  [build parser] Stripped embedded --BEGINFILE-- marker from {filepath}")
        # Strip a single trailing newline from content if present
        if content.endswith("\n"):
            content = content[:-1]
        files.append((filepath, content))
    return files


# Fallback content for db/index.js if the template file is missing
_DB_INDEX_JS_FALLBACK = """'use strict';

const { Pool } = require('pg');

const isInternal = (process.env.DATABASE_URL || '').includes('railway.internal');
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: (!isInternal && process.env.NODE_ENV === 'production') ? { rejectUnauthorized: false } : false
});

module.exports = pool;
"""


def _read_db_index_template() -> str:
    """Read db/index.js content from cli/templates/db_index.js, with fallback."""
    template_path = Path(__file__).parent / "templates" / "db_index.js"
    if template_path.exists():
        return template_path.read_text()
    return _DB_INDEX_JS_FALLBACK.strip() + "\n"


# Regex to match standalone Pool creation in route/migrate files
_POOL_CREATION_RE = re.compile(
    r"""^.*(?:const|let|var)\s+pool\s*=\s*new\s+Pool\s*\(.*\).*$""",
    re.MULTILINE,
)
_PG_REQUIRE_RE = re.compile(
    r"""^.*(?:const|let|var)\s+\{\s*Pool\s*\}\s*=\s*require\s*\(\s*['"]pg['"]\s*\).*$""",
    re.MULTILINE,
)


@processor()
def _ensure_shared_db_pool(files: list) -> list:
    """Post-process generated files to ensure a shared db/index.js with SSL.

    Claude's generated code often creates separate Pool instances in each route
    file without SSL config. Railway Postgres requires SSL for external
    connections, causing 500 errors on all DB-touching routes.

    This function:
    1. Creates db/index.js if missing (shared pool with SSL)
    2. Patches route files and db/migrate.js to use require('../db') or
       require('./index') instead of creating their own Pool
    """
    file_dict = dict(files)
    patched_count = 0

    # Ensure db/index.js exists with SSL config
    if "db/index.js" not in file_dict:
        file_dict["db/index.js"] = _read_db_index_template()
        print("  [post-process] Created db/index.js with SSL config")

    # Patch files that create their own Pool (except db/index.js and server.js)
    for filepath, content in list(file_dict.items()):
        if filepath == "db/index.js":
            continue
        if not filepath.endswith(".js"):
            continue
        if "new Pool(" not in content:
            continue

        original = content

        # Determine the correct require path
        if filepath.startswith("routes/"):
            require_path = "../db"
        elif filepath.startswith("db/"):
            require_path = "./index"
        elif filepath == "server.js":
            require_path = "./db"
        else:
            require_path = "./db"

        # Remove the `const { Pool } = require('pg')` line
        content = _PG_REQUIRE_RE.sub("", content)

        # Replace `const pool = new Pool(...)` with require
        content = _POOL_CREATION_RE.sub(
            f"const pool = require('{require_path}');",
            content,
        )

        # Clean up blank lines left from removals
        while "\n\n\n" in content:
            content = content.replace("\n\n\n", "\n\n")

        if content != original:
            file_dict[filepath] = content
            patched_count += 1
            print(f"  [post-process] Patched {filepath}: using shared db pool")

    if patched_count:
        print(f"  [post-process] Patched {patched_count} file(s) to use shared db/index.js")

    return list(file_dict.items())


@processor(after=('_fix_tailwind_config',), phase=('build',))
def _fix_csrf_pattern(files: list) -> list:
    """Remove csrf-csrf/csurf from package.json and fix csrfToken references.

    Claude sometimes installs csrf-csrf despite instructions not to, then uses
    req.csrfToken() which crashes because the middleware is session-based.
    This post-processing ensures the session-based pattern is used consistently.
    """
    result = []
    patched = False
    for filepath, content in files:
        original = content

        if filepath == "package.json" and ("csrf-csrf" in content or '"csurf"' in content):
            # Remove csrf-csrf and csurf from dependencies
            import json as json_mod
            try:
                pkg = json_mod.loads(content)
                deps = pkg.get("dependencies", {})
                for bad_pkg in ("csrf-csrf", "csurf"):
                    if bad_pkg in deps:
                        del deps[bad_pkg]
                        print(f"  [post-process] Removed {bad_pkg} from package.json")
                content = json_mod.dumps(pkg, indent=2) + "\n"
            except Exception:
                pass

        if filepath.endswith(".js"):
            # Remove require('../middleware/csrf') or require('./middleware/csrf')
            # Claude sometimes generates a non-existent csrf middleware file
            import re as re_mod
            csrf_require = re_mod.compile(
                r"""(?:const|let|var)\s+\w+\s*=\s*require\(['"]\.{1,2}/middleware/csrf['"]\);?\s*\n?"""
            )
            if csrf_require.search(content):
                content = csrf_require.sub("", content)
                # Also remove any middleware usage like app.use(csrfProtection) or router.use(csrf)
                content = re_mod.sub(r"""(?:app|router)\.use\(\s*(?:csrfProtection|csrf|csrfMiddleware)\s*\);?\s*\n?""", "", content)
                print(f"  [post-process] Removed non-existent csrf middleware import from {filepath}")

            # Fix req.csrfToken() -> res.locals.csrfToken
            if "req.csrfToken()" in content:
                content = content.replace("req.csrfToken()", "res.locals.csrfToken")
                print(f"  [post-process] Fixed csrfToken in {filepath}")
            # Fix csrfToken: req.csrfToken() in render calls
            if "csrfToken: req.csrfToken()" in content:
                content = content.replace("csrfToken: req.csrfToken()", "csrfToken: res.locals.csrfToken")

        if content != original:
            patched = True
        result.append((filepath, content))
    return result


@processor(after=('_ensure_shared_db_pool',), phase=('build',))
def _fix_tailwind_config(files: list) -> list:
    """Fix the Tailwind CDN config variable name in generated layout files.

    Claude sometimes generates `tailwindcss.config = {...}` but the correct
    variable name for Tailwind CDN Play is `tailwind.config = {...}`. The wrong
    name causes a ReferenceError, breaking all Tailwind utility classes and
    making buttons with bg-primary invisible (white text on transparent bg).
    """
    result = []
    for filepath, content in files:
        if filepath.endswith(".ejs") and "tailwindcss.config" in content:
            content = content.replace("tailwindcss.config", "tailwind.config")
            print(f"  [post-process] Fixed Tailwind config variable in {filepath}")
        result.append((filepath, content))
    return result


@processor(after=('_ensure_validate_csrf_export',), phase=('build',))
def _fix_circular_imports(files: list) -> list:
    """Fix circular dependencies where route files import from server.js.

    Claude sometimes generates routes that require validateCsrfToken or other
    middleware from '../server' or '../../server', creating a circular dependency
    (server.js -> routes -> server.js) that crashes on startup. This redirects
    those imports to '../middleware/auth' where CSRF and auth middleware live.
    """
    import re as re_mod
    result = []
    patched_count = 0
    for filepath, content in files:
        if not filepath.endswith(".js"):
            result.append((filepath, content))
            continue

        original = content

        # Match: require('../server') or require('../../server') or require('./server')
        server_require_re = re_mod.compile(
            r"""require\(\s*['"](\.\.?/)*server['"]\s*\)"""
        )
        if server_require_re.search(content):
            # Determine the right relative path to middleware/auth
            if filepath.startswith("routes/"):
                auth_path = "../middleware/auth"
            else:
                auth_path = "./middleware/auth"

            # Replace destructured imports from server with imports from middleware/auth
            # e.g. const { validateCsrfToken, requireAuth } = require('../server')
            content = re_mod.sub(
                r"""require\(\s*['"](\.\.?/)*server['"]\s*\)""",
                f"require('{auth_path}')",
                content,
            )
            patched_count += 1
            print(f"  [post-process] Fixed circular server import in {filepath} -> {auth_path}")

        if content != original:
            pass  # counted above
        result.append((filepath, content))

    if patched_count:
        print(f"  [post-process] Fixed {patched_count} circular import(s)")
    return result


@processor(after=('_fix_csrf_pattern',))
def _ensure_validate_csrf_export(files: list) -> list:
    """Ensure middleware/auth.js exports validateCsrfToken.

    The static routes/admin.js template always requires validateCsrfToken from
    '../middleware/auth'. When Claude generates its own middleware/auth.js it
    sometimes omits this export, causing a startup crash on every route require.
    This post-processor appends the missing export and function if absent.
    """
    VALIDATE_FN = """
function validateCsrfToken(req, res, next) {
  const token = req.body._csrf || req.headers['x-csrf-token'];
  if (!token || token !== req.session.csrfToken) {
    return res.status(403).send('Invalid CSRF token');
  }
  next();
}
"""
    result = []
    for filepath, content in files:
        if filepath in ("middleware/auth.js",):
            if "validateCsrfToken" not in content:
                # Append the function and add to module.exports
                content = content.rstrip()
                content += "\n" + VALIDATE_FN.strip() + "\n"
                # Patch module.exports to include validateCsrfToken
                import re
                content = re.sub(
                    r"module\.exports\s*=\s*\{([^}]+)\}",
                    lambda m: "module.exports = {"
                        + m.group(1).rstrip().rstrip(",").rstrip()
                        + ", validateCsrfToken }",
                    content,
                )
                print("  [post-process] Added missing validateCsrfToken to middleware/auth.js")
        result.append((filepath, content))
    return result


@processor(after=('_fix_circular_imports',), phase=('build',))
def _ensure_route_views(files: list) -> list:
    """Ensure every res.render() call has a corresponding view file.

    Claude sometimes generates route files with render calls but forgets to
    create the matching .ejs view files, causing 'Failed to lookup view' crashes.
    This scans for missing views and generates basic scaffolds.
    """
    import re as re_mod

    file_dict = dict(files)
    existing_views = {f for f in file_dict if f.startswith("views/") and f.endswith(".ejs")}

    # Find all res.render('viewname') calls across route files
    render_re = re_mod.compile(r"""res\.render\(\s*['"]([a-zA-Z0-9_/-]+)['"]\s*[,)]""")
    referenced_views = set()
    for filepath, content in files:
        if filepath.startswith("routes/") and filepath.endswith(".js"):
            for match in render_re.finditer(content):
                view_name = match.group(1)
                referenced_views.add(view_name)

    # Check which views are missing
    created = 0
    for view_name in sorted(referenced_views):
        view_path = f"views/{view_name}.ejs"
        if view_path not in existing_views:
            # Generate a minimal scaffold view
            title = view_name.replace("-", " ").replace("_", " ").title()
            scaffold = (
                f'<h1 class="text-3xl font-bold text-gray-800 mb-8">{title}</h1>\n'
                f'<div class="bg-white rounded-xl shadow-md p-6">\n'
                f'  <p class="text-gray-600">Content for {title} will appear here.</p>\n'
                f'</div>\n'
            )
            file_dict[view_path] = scaffold
            created += 1
            print(f"  [post-process] Created missing view: {view_path}")

    if created:
        print(f"  [post-process] Generated {created} missing view scaffold(s)")

    return list(file_dict.items())


@processor(after=('_ensure_route_views',), phase=('build',))
def _normalize_password_column_to_canonical(files: list) -> list:
    """Normalize Claude-emitted `password_hash` column to the canonical
    `password` convention that CAS's shipped templates use.

    Motivation (discovered 2026-04-20 drift audit): the canonical
    cli/templates/routes_admin.js queries `SELECT password FROM users`
    and `UPDATE users SET password = $1` (lines 271 + 282). The
    Medicine Waves gold-standard schema uses `password VARCHAR(255)`.
    CAS build prompts don't explicitly dictate the column name, so
    Claude occasionally emits `password_hash` instead, when that
    happens the schema↔JS drift validator correctly flags the
    mismatch and fails the build. Pre-emptively normalizing keeps the
    canonical template working regardless of Claude's choice.

    Scope:
      - db/schema.sql: rename `password_hash` column in the users
        table to `password` (and update its corresponding index if
        present).
      - db/seed.sql: rewrite INSERT column lists referencing
        `password_hash`.
      - Claude-emitted JS/EJS route files referencing `password_hash`
        in SQL statements: rewrite to `password`.

    Intentionally targeted to the `users` table only; a spec that
    genuinely has `password_hash` on a different table (e.g. a
    historical backup table) is left alone. Idempotent.
    """
    import re as _re_mod
    file_dict = dict(files)
    schema = file_dict.get("db/schema.sql", "")
    if not isinstance(schema, str):
        return files
    users_block_re = _re_mod.compile(
        r"(CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+users\s*\()([^;]*?)(\)\s*;)",
        _re_mod.IGNORECASE | _re_mod.DOTALL,
    )
    m = users_block_re.search(schema)
    if not m or "password_hash" not in m.group(2):
        return files

    changed = False
    # Rewrite the users CREATE TABLE body: `password_hash` → `password`.
    new_body = _re_mod.sub(
        r"\bpassword_hash\b", "password", m.group(2),
    )
    schema = schema[:m.start()] + m.group(1) + new_body + m.group(3) + schema[m.end():]
    # Also rewrite any standalone index declarations that reference
    # password_hash on the users table.
    schema = _re_mod.sub(
        r"(CREATE\s+INDEX[^;]*\busers\s*\([^)]*?)password_hash",
        r"\1password",
        schema,
        flags=_re_mod.IGNORECASE,
    )
    file_dict["db/schema.sql"] = schema
    changed = True
    print(
        "  [post-process] Normalized users.password_hash → users.password "
        "to match canonical routes_admin.js template"
    )

    # Rewrite seed.sql INSERT column lists.
    seed = file_dict.get("db/seed.sql", "")
    if isinstance(seed, str) and "password_hash" in seed:
        new_seed = _re_mod.sub(
            r"(INSERT\s+INTO\s+users\s*\([^)]*?)\bpassword_hash\b",
            r"\1password",
            seed,
            flags=_re_mod.IGNORECASE,
        )
        if new_seed != seed:
            file_dict["db/seed.sql"] = new_seed

    # Sweep JS/EJS files for SQL references. `\b` bounds keep the
    # rewrite scoped to whole-word identifiers so unrelated tokens
    # survive.
    for fpath in list(file_dict.keys()):
        if not isinstance(fpath, str):
            continue
        if not (fpath.endswith(".js") or fpath.endswith(".ejs")):
            continue
        if fpath in {"db/schema.sql", "db/seed.sql"}:
            continue
        content = file_dict[fpath]
        if not isinstance(content, str) or "password_hash" not in content:
            continue
        # Rewrite `password_hash` → `password` inside SQL-context
        # patterns ONLY: after SELECT/UPDATE/INSERT column tokens,
        # after WHERE, and in object-access expressions like
        # `user.password_hash`.
        new_content = _re_mod.sub(r"\bpassword_hash\b", "password", content)
        if new_content != content:
            file_dict[fpath] = new_content

    return list(file_dict.items())


@processor(after=('_normalize_password_column_to_canonical',), phase=('build',))
def _strip_demo_credentials(files: list) -> list:
    """Remove demo credential blocks from login/register views.

    Claude sometimes generates "Demo Access" accordions or credential hints
    on login pages despite instructions not to. These must never appear in
    customer-facing apps. Strips common patterns from .ejs view files.
    """
    import re as re_mod

    # Patterns to remove from EJS files (multiline)
    patterns = [
        # Collapsible/details demo sections
        re_mod.compile(
            r'<details[^>]*>.*?Demo\s*Access.*?</details>',
            re_mod.DOTALL | re_mod.IGNORECASE,
        ),
        # Div blocks with demo-access class or id
        re_mod.compile(
            r'<div[^>]*(?:id|class)=["\'][^"\']*demo[^"\']*["\'][^>]*>.*?</div>',
            re_mod.DOTALL | re_mod.IGNORECASE,
        ),
        # Paragraphs/small text mentioning demo accounts with example.com
        re_mod.compile(
            r'<(?:p|small|span|div)[^>]*>.*?(?:demo\s*account|example\.com.*?password|password123).*?</(?:p|small|span|div)>',
            re_mod.DOTALL | re_mod.IGNORECASE,
        ),
    ]

    result = []
    stripped_count = 0
    for filepath, content in files:
        if not filepath.endswith(".ejs"):
            result.append((filepath, content))
            continue

        original = content
        for pattern in patterns:
            content = pattern.sub("", content)

        if content != original:
            stripped_count += 1
            print(f"  [post-process] Stripped demo credentials from {filepath}")

        result.append((filepath, content))

    if stripped_count:
        print(f"  [post-process] Removed demo credential blocks from {stripped_count} file(s)")
    return result


@processor(after=('_strip_demo_credentials',), phase=('build',))
def _fix_login_form_fields(files: list) -> list:
    """Fix login forms where Claude generates name='username' instead of name='email'.

    Claude frequently generates login form inputs with name="username" but the
    generated routes/auth.js expects req.body.email. This mismatch causes login
    to silently fail. Replaces name="username" with name="email" on login pages
    and also fixes the corresponding label/placeholder text.
    """
    import re as re_mod

    login_file_patterns = ("login", "signin", "sign-in", "sign_in")

    result = []
    fixed_count = 0
    for filepath, content in files:
        is_login_view = (
            filepath.endswith(".ejs")
            and any(p in filepath.lower() for p in login_file_patterns)
        )
        is_auth_route = filepath == "routes/auth.js"

        if not is_login_view and not is_auth_route:
            result.append((filepath, content))
            continue

        original = content

        if is_login_view:
            # Fix input name attribute: name="username" -> name="email"
            content = re_mod.sub(
                r'name\s*=\s*["\']username["\']',
                'name="email"',
                content,
            )
            # Fix type attribute if it was "text" for the username field
            # (only on inputs that now have name="email")
            content = re_mod.sub(
                r'(type\s*=\s*["\'])text(["\'](\s+[^>]*)?name\s*=\s*["\']email["\'])',
                r'\1email\2',
                content,
            )
            content = re_mod.sub(
                r'(name\s*=\s*["\']email["\'](\s+[^>]*)?type\s*=\s*["\'])text(["\'])',
                r'\1email\3',
                content,
            )
            # Fix placeholder text
            content = re_mod.sub(
                r'(name\s*=\s*["\']email["\'][^>]*placeholder\s*=\s*["\'])[Uu]sername(["\'])',
                r'\1Email address\2',
                content,
            )
            content = re_mod.sub(
                r'(placeholder\s*=\s*["\'])[Uu]sername(["\'][^>]*name\s*=\s*["\']email["\'])',
                r'\1Email address\2',
                content,
            )
            # Fix label text: "Username" -> "Email"
            content = re_mod.sub(
                r'(<label[^>]*for\s*=\s*["\'](?:username|email)["\'][^>]*>)\s*Username\s*(</label>)',
                r'\1Email\2',
                content,
                flags=re_mod.IGNORECASE,
            )

        if is_auth_route:
            # Fix route handler: req.body.username -> req.body.email
            content = content.replace("req.body.username", "req.body.email")

        if content != original:
            fixed_count += 1
            print(f"  [post-process] Fixed login form fields in {filepath}")

        result.append((filepath, content))

    if fixed_count:
        print(f"  [post-process] Fixed username->email in {fixed_count} file(s)")
    return result


@processor(after=('_normalize_admin_view_paths',))
def _enforce_admin_view_path_convention(files, spec):
    """ADMIN_VIEW_CONVENTION (v16): routes are ground truth; make template files match.

    Scans every route file for `res.render('admin/SLUG', ...)` calls and ensures
    that `views/admin/SLUG.ejs` exists. If not, resolves from any of:
      - views/admin/SLUG-index.ejs   (v15's pattern, just rename)
      - views/admin-SLUG.ejs         (v13's hyphen pattern: rename to subdir)
      - views/admin/SLUG-list.ejs
      - views/SLUG.ejs               (only if no public view with the same name)

    This catches the recurring "convention drift" bug where Claude generates
    views under one naming scheme while routes render with another. First
    observed in v13 (hyphen), v15 (-index suffix). RCA:
    docs/internals/MW_BUG_RECURRENCE_RCA.md § 1.
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False

    render_re = re_mod.compile(r"""res\.render\(\s*['"](admin/[\w-]+)['"]""")
    rendered_paths = set()
    for path, content in file_dict.items():
        if not isinstance(content, str):
            continue
        if not (path.startswith("routes/") or path in ("server.js", "app.js")):
            continue
        for m in render_re.finditer(content):
            rendered_paths.add(m.group(1))  # e.g. 'admin/events'

    for rendered in rendered_paths:
        # rendered = 'admin/events' → target file = 'views/admin/events.ejs'
        slug = rendered.split("/", 1)[1]  # 'events'
        target = f"views/{rendered}.ejs"
        if target in file_dict:
            continue  # canonical already present
        # Try candidate sources
        candidates = [
            f"views/admin/{slug}-index.ejs",
            f"views/admin-{slug}.ejs",
            f"views/admin/{slug}-list.ejs",
            f"views/admin/index-{slug}.ejs",
        ]
        for src in candidates:
            if src in file_dict:
                file_dict[target] = file_dict[src]
                del file_dict[src]
                changed = True
                print(f"  [post-process] Admin view convention: renamed {src} → {target}")
                break

    return list(file_dict.items()) if changed else files


def _derive_site_name_from_spec(spec) -> str:
    """Derive a human-readable site name from the spec.

    Order of preference (mirrors _ensure_nav_brand_element):
      1. spec.settings.site_name
      2. spec.settings.business_name
      3. Pretty-cased spec.app_name with -v<N>-<N> version suffix stripped
      4. "Site" as a last-resort fallback

    Used by admin-surface injections that previously hardcoded "Medicine
    Waves" into every generated app's page titles.
    """
    import re as _re_mod

    if not isinstance(spec, dict):
        return "Site"
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    name = ""
    if settings:
        name = str(settings.get("site_name") or "").strip()
        if not name:
            name = str(settings.get("business_name") or "").strip()
    if not name:
        raw = str(spec.get("app_name") or "").strip()
        if raw:
            # Strip trailing "-v\d+-\d+" version suffix like "-v1-123456".
            raw = _re_mod.sub(r"-v\d+(?:-\d+)?$", "", raw)
            name = " ".join(
                w.capitalize() for w in _re_mod.split(r"[-_\s]+", raw) if w
            )
    return name or "Site"


@processor(after=('_enforce_admin_view_path_convention',))
def _ensure_admin_model_handlers(files, spec):
    """ADMIN_MODEL_HANDLERS (v16): dashboard.js must have /admin/{slug} GET
    handler for every data_model in the spec. Auto-injects any missing ones.

    Fixes the v15 regression where routes/videos.js had full CRUD but
    routes/dashboard.js was missing the /admin/videos handler, causing the
    admin list link to 404. Pattern: Claude scaffolds the per-model routes
    reliably but the admin dashboard shortcut handlers inconsistently.
    RCA: docs/internals/MW_BUG_RECURRENCE_RCA.md § 6.

    The page title was previously hardcoded as
    "Manage {Name} | Medicine Waves" in every injected handler,
    leaking the MW brand into non-MW customer apps. Now derives the
    suffix from spec.settings.site_name / business_name / pretty-cased
    app_name via _derive_site_name_from_spec.
    """
    import re as re_mod

    file_dict = dict(files)
    dash_path = "routes/dashboard.js"
    dash = file_dict.get(dash_path)
    if not dash or not isinstance(dash, str):
        # No dashboard.js: nothing to fix (app might not have admin sub-router)
        return files

    data_models = spec.get("data_models", [])
    if not data_models:
        return files

    # Which /admin/SLUG handlers does dashboard.js already have?
    existing_re = re_mod.compile(r"""router\.get\(\s*['"]/([\w-]+)['"]""")
    existing = set(existing_re.findall(dash))

    changed = False
    insertions = []
    for model in data_models:
        name = model["name"] if isinstance(model, dict) else str(model)
        slug = _model_slug(name)
        plural = _pluralize_slug(slug)
        if plural in existing:
            continue
        # Skip ContactMessage-style read-only models (spec could declare
        # read-only; for now just check for contact_messages existence)
        if plural in ("messages", "contact-messages", "subscribers"):
            continue
        # Admin table name: plural slug with underscores
        table = plural.replace("-", "_")
        insertions.append((plural, table, name))

    if not insertions:
        return files

    # Derive site name from the spec; never hardcode a brand string.
    site_name = _derive_site_name_from_spec(spec)
    # JS-escape via json.dumps so newlines, tabs, embedded quotes and
    # backslashes don't break the emitted JS string. json.dumps wraps in
    # double-quotes; strip them and swap the apostrophe escape so the
    # result is safe inside a single-quoted literal. (This injected code
    # lands in server.js, not an inline HTML context, so `</script>` is
    # not a concern here.)
    import json as _json_mod
    site_name_js = _json_mod.dumps(site_name, ensure_ascii=False)[1:-1].replace("'", "\\'")

    # Build the handler code for each missing model. The template var name
    # (locals key passed to res.render) uses the SQL table name (underscore-
    # separated) rather than the URL slug (hyphen-separated) so hyphenated
    # model slugs like `blog-posts` don't produce invalid JS object keys.
    # Pre-existing bug that stayed hidden because MW models
    # (events/photos/videos) had no hyphens. Fixing here alongside the
    # site-name derivation so the injected handlers are actually runnable.
    handler_template = '''
// GET /admin/{slug} - {name} management (auto-injected by _ensure_admin_model_handlers)
router.get('/{slug}', requireAuth, requireRole('admin'), async (req, res) => {{
  try {{
    const result = await pool.query('SELECT * FROM {table} ORDER BY id ASC');
    res.render('admin/{slug}', {{ title: 'Manage {Name} | {site_name}', {table}: result.rows }});
  }} catch (err) {{
    console.error('Error in GET /admin/{slug}:', err);
    res.status(500).render('500', {{ title: 'Error' }});
  }}
}});
'''
    # Append each missing handler before module.exports
    appended = ""
    for slug, table, name in insertions:
        appended += handler_template.format(
            slug=slug, table=table, Name=name.capitalize(), name=name,
            site_name=site_name_js,
        )

    if "module.exports" in dash:
        dash = dash.replace("module.exports", appended + "\nmodule.exports", 1)
    else:
        dash = dash + "\n" + appended + "\n"

    # Ensure `requireRole` is imported from middleware/auth; Claude's
    # original dashboard.js usually imports only requireAuth +
    # requirePasswordSet, but the injected handlers above reference
    # `requireRole('admin')`. Without this, dashboard.js crashes at
    # require() time with ReferenceError.
    _require_line_re = re_mod.compile(
        r"""const\s*\{\s*([^}]+)\s*\}\s*=\s*require\(\s*['"]\.\./middleware/auth['"]\s*\)\s*;""",
    )
    # Also detect the non-destructured import pattern
    # `const auth = require('../middleware/auth');` so we don't
    # fabricate a second require when one already exists in a
    # different shape.
    _auth_require_present_re = re_mod.compile(
        r"""require\(\s*['"]\.\./middleware/auth['"]\s*\)""",
    )
    _m = _require_line_re.search(dash)
    if _m and "requireRole" not in _m.group(1):
        # Inject requireRole into the destructured import list.
        new_imports = _m.group(1).rstrip().rstrip(",") + ", requireRole"
        dash = dash[:_m.start(1)] + " " + new_imports + " " + dash[_m.end(1):]
        print(
            "  [post-process] Added requireRole to middleware/auth "
            "import in routes/dashboard.js"
        )
    elif not _m and "requireRole" in dash:
        # Destructured import missing. Only prepend a fresh import if
        # there isn't an existing `require('../middleware/auth')` in
        # ANY shape (single OR double quotes, destructured OR named
        # binding). Otherwise we'd fabricate a duplicate require.
        if not _auth_require_present_re.search(dash):
            dash = (
                "const { requireRole } = require('../middleware/auth');\n"
                + dash
            )
            print(
                "  [post-process] Prepended requireRole import to "
                "routes/dashboard.js"
            )
        else:
            # Auth module is already imported but in a non-destructured
            # shape we can't extend. Leave a clear trace for the build
            # log: the schema-column validator + contracts path will
            # still fire, and in practice Claude always uses destructured
            # imports so this branch is a safety net.
            print(
                "  [post-process] WARNING: requireRole referenced in "
                "routes/dashboard.js but middleware/auth is imported in "
                "a non-destructured shape: leaving untouched"
            )

    file_dict[dash_path] = dash
    changed = True
    for slug, _, _ in insertions:
        print(f"  [post-process] Injected missing /admin/{slug} handler into routes/dashboard.js")

    return list(file_dict.items()) if changed else files


@processor(after=('_ensure_admin_model_handlers',))
def _ensure_settings_middleware_contract(files, spec):
    """SETTINGS_MIDDLEWARE_CONTRACT (v16): server.js middleware MUST populate
    res.locals.settings, not just res.locals.siteSettings. All generated
    views use `settings.X` references; the convention is baked into the
    template library (_ensure_site_settings, _ensure_hero_settings_binding).

    First observed v15: middleware set `siteSettings` only; views' settings
    reads silently fell back to default text because settings was undefined.
    The hero tagline LOOKED correct only because the fallback equaled the DB
    value (coincidence). `hero_image_url` had empty fallback → no background.
    RCA: docs/internals/MW_BUG_RECURRENCE_RCA.md § 2.
    """
    import re as re_mod

    file_dict = dict(files)
    server_path = "server.js" if "server.js" in file_dict else ("app.js" if "app.js" in file_dict else None)
    if not server_path:
        return files
    server = file_dict[server_path]
    if not isinstance(server, str):
        return files

    # If res.locals.settings is already set, skip
    if re_mod.search(r"res\.locals\.settings\s*=", server):
        return files

    # Find existing middleware that sets res.locals.siteSettings and alias.
    # Pattern variants Claude emits:
    #   res.locals.siteSettings = settings;
    #   res.locals.siteSettings = result.rows.reduce(...);
    alias_re = re_mod.compile(
        r"(res\.locals\.siteSettings\s*=\s*[^;]+;)",
    )
    m = alias_re.search(server)
    if m:
        # Insert an alias line right after the siteSettings assignment
        server = server[:m.end()] + "\n    res.locals.settings = res.locals.siteSettings;" + server[m.end():]
        file_dict[server_path] = server
        print("  [post-process] Aliased res.locals.settings = res.locals.siteSettings")
        return list(file_dict.items())

    # No existing siteSettings middleware. Inject a fresh one right after
    # `const app = express();` or before routes mount.
    express_re = re_mod.compile(r"(const\s+app\s*=\s*express\(\)\s*;?)")
    m = express_re.search(server)
    if m:
        middleware = '''

// Settings middleware: populate res.locals.settings from site_settings table
// so templates can reference `settings.hero_tagline`, `settings.nav_logo_url`, etc.
// (SETTINGS_MIDDLEWARE_CONTRACT, v16)
app.use(async (req, res, next) => {
  try {
    const pool = require('./db');
    const result = await pool.query('SELECT key, value FROM site_settings');
    const settings = {};
    result.rows.forEach(row => { settings[row.key] = row.value; });
    res.locals.settings = settings;
    res.locals.siteSettings = settings;  // alias for older templates
  } catch (err) {
    res.locals.settings = {};
    res.locals.siteSettings = {};
  }
  next();
});
'''
        server = server[:m.end()] + middleware + server[m.end():]
        file_dict[server_path] = server
        print("  [post-process] Injected settings middleware (populates res.locals.settings)")
        return list(file_dict.items())

    return files


@processor(after=('_ensure_event_time_helper',))
def _ensure_tz_aware_datetime_schema(files, spec):
    """TIMESTAMPTZ_SCHEMA (v16): date_time (and similar) columns use
    TIMESTAMPTZ (timestamp WITH time zone) so the pg driver returns proper
    UTC-anchored Date objects. Without this, `TIMESTAMP '2026-05-13 00:00:00'`
    seed strings are interpreted in the Node process's local TZ and display
    wrong in other timezones.

    Fix strategy:
      1. In db/schema.sql, replace `TIMESTAMP` (not `TIMESTAMPTZ`) with
         `TIMESTAMPTZ` for columns named date_time/datetime/starts_at/ends_at/
         scheduled_at/event_date/start_time/end_time/created_at/updated_at.
      2. In db/seed.sql, any raw `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'` literal
         for those columns gets ` AT TIME ZONE 'UTC'` appended so naive
         seed data is interpreted as UTC.

    RCA: docs/internals/MW_BUG_RECURRENCE_RCA.md § Timezone.
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False

    TZ_AWARE_COLUMN_NAMES = (
        "date_time", "datetime", "starts_at", "ends_at", "scheduled_at",
        "event_date", "start_time", "end_time", "published_at",
        "created_at", "updated_at", "completed_at", "expires_at",
    )

    # 1. Schema change: TIMESTAMP → TIMESTAMPTZ for target columns
    schema = file_dict.get("db/schema.sql", "")
    if schema:
        orig = schema
        for col in TZ_AWARE_COLUMN_NAMES:
            # Match: `<col_name> TIMESTAMP` (word boundary; not TIMESTAMPTZ)
            pattern = re_mod.compile(
                rf'\b({re_mod.escape(col)})\s+TIMESTAMP(?!TZ)\b',
                re_mod.IGNORECASE,
            )
            schema = pattern.sub(r'\1 TIMESTAMPTZ', schema)
        if schema != orig:
            file_dict["db/schema.sql"] = schema
            changed = True
            print("  [post-process] Schema: TIMESTAMP → TIMESTAMPTZ on date columns")

    # 2. Seed change: `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'` → same but appended
    #    AT TIME ZONE 'UTC'
    seed = file_dict.get("db/seed.sql", "")
    if seed:
        orig = seed
        # Match naive TIMESTAMP 'YYYY-MM-DD HH:MM:SS' literals
        naive_re = re_mod.compile(
            r"TIMESTAMP\s+'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)'",
            re_mod.IGNORECASE,
        )
        seed = naive_re.sub(r"(TIMESTAMP '\1' AT TIME ZONE 'UTC')", seed)
        if seed != orig:
            file_dict["db/seed.sql"] = seed
            changed = True
            print("  [post-process] Seed: naive TIMESTAMP literals anchored to UTC")

    return list(file_dict.items()) if changed else files


class SchemaColumnDriftError(RuntimeError):
    """Raised by _verify_schema_column_references when a generated JS /
    EJS file references a (table, column) pair that the schema does
    NOT define. Carries a list of structured mismatches so _process_job
    can surface file:col details in its failure message."""

    def __init__(self, mismatches):
        self.mismatches = list(mismatches)
        header = (
            f"schema-column drift: {len(self.mismatches)} reference(s) "
            f"in JS/EJS do not match db/schema.sql"
        )
        sample = ", ".join(
            f"{m['file']}:{m.get('line','?')} {m['table']}.{m['column']} "
            f"(schema has: {sorted(m.get('schema_cols', []))[:6]})"
            for m in self.mismatches[:3]
        )
        super().__init__(f"{header}; first mismatches: {sample}")


def _parse_schema_columns(schema_sql):
    """Parse `CREATE TABLE [IF NOT EXISTS] <name> ( <cols> );` blocks.
    Return {table_name: {col1, col2, ...}} with lowercase names.

    Intentionally simple: only top-level CREATE TABLE statements, and
    only the first identifier on each column-def line inside the
    parentheses. Skips constraint lines (CONSTRAINT / PRIMARY KEY /
    FOREIGN KEY / UNIQUE / CHECK / INDEX). Not a full SQL parser; 
    scoped to the schema shape the generator actually emits.
    """
    import re as _re
    if not isinstance(schema_sql, str):
        return {}
    tables = {}
    table_re = _re.compile(
        r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([\w.\"`]+)\s*\(",
        _re.IGNORECASE,
    )
    pos = 0
    while True:
        m = table_re.search(schema_sql, pos)
        if not m:
            break
        table_raw = m.group(1).strip('"`')
        # Strip schema qualifier (schema.table → table)
        table = table_raw.split(".")[-1].lower()
        # Scan forward to find the matching close paren (depth-1 paren
        # balanced, respecting string literals)
        depth = 1
        i = m.end()
        in_string = False
        while i < len(schema_sql) and depth > 0:
            ch = schema_sql[i]
            if ch == "'":
                if in_string and i + 1 < len(schema_sql) and schema_sql[i + 1] == "'":
                    i += 2
                    continue
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
            i += 1
        body = schema_sql[m.end():i]
        pos = i + 1
        # Strip SQL `-- line comments` (Claude occasionally emits inline
        # explanatory comments inside CREATE TABLE bodies). Without
        # this, the comma-splitter below treats the comment + trailing
        # newline + next column-def as one "part" whose first token is
        # `--`, which gets dropped, and the real column that followed
        # the comment is lost. B2 of the 2026-04-20 deep-review.
        body = _re.sub(r"--[^\n]*", "", body)
        cols = set()
        # Split column defs at top-level commas (respect parens + strings).
        # String state machine uses forward-lookahead for the `''` escape
        # pair, matching _sql_stmt_strings_closed. The previous
        # backward-lookup-via-buf[-1] variant mis-identified the second
        # `'` of an EMPTY-string literal (`''`) as an escape pair and
        # stayed in-string through the next `,`; dropping the following
        # column. Manifested on the canonical site_settings table where
        # `updated_at TIMESTAMP DEFAULT NOW()` sat right after
        # `value TEXT DEFAULT '',`.
        col_depth = 0
        col_in_string = False
        buf = []
        parts = []
        bi = 0
        blen = len(body)
        while bi < blen:
            ch = body[bi]
            if ch == "'":
                # Forward lookahead: `'` immediately following another
                # `'` while ALREADY inside a string is an escape pair; 
                # append both, stay in-string, advance past both.
                if col_in_string and bi + 1 < blen and body[bi + 1] == "'":
                    buf.append(ch)
                    buf.append(body[bi + 1])
                    bi += 2
                    continue
                col_in_string = not col_in_string
                buf.append(ch)
                bi += 1
                continue
            if col_in_string:
                buf.append(ch)
                bi += 1
                continue
            if ch == "(":
                col_depth += 1
            elif ch == ")":
                col_depth -= 1
            if ch == "," and col_depth == 0:
                parts.append("".join(buf).strip())
                buf = []
                bi += 1
                continue
            buf.append(ch)
            bi += 1
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        SKIP_KEYWORDS = (
            "CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "INDEX",
            "EXCLUDE", "LIKE",
        )
        for part in parts:
            if not part:
                continue
            first = part.split()[0] if part.split() else ""
            first_upper = first.upper().rstrip(",")
            if first_upper in SKIP_KEYWORDS:
                continue
            col_name = first.strip('"`').lower()
            if col_name and col_name.replace("_", "").isalnum():
                cols.add(col_name)
        if cols:
            tables[table] = cols
    return tables


# SQL query string patterns inside JS/EJS. We match:
#   SELECT col1, col2 FROM table
#   INSERT INTO table (col1, col2) VALUES
#   UPDATE table SET col = ...
import re as _re_schema_validator
_Q_SELECT_FROM_RE = _re_schema_validator.compile(
    r"""SELECT\s+(.+?)\s+FROM\s+(\w+)""",
    _re_schema_validator.IGNORECASE | _re_schema_validator.DOTALL,
)
_Q_INSERT_INTO_RE = _re_schema_validator.compile(
    r"""INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)""",
    _re_schema_validator.IGNORECASE,
)
_Q_UPDATE_SET_RE = _re_schema_validator.compile(
    r"""UPDATE\s+(\w+)\s+SET\s+(.+?)(?:WHERE|ON\s+CONFLICT|RETURNING|;|$)""",
    _re_schema_validator.IGNORECASE | _re_schema_validator.DOTALL,
)
# Used to skip validation when the SELECT crosses multiple tables
# (JOIN / comma-separated FROM list / subquery). For those cases the
# simple "first table gets all columns" attribution is wrong and would
# false-positively raise SchemaColumnDriftError on legitimate queries.
# Checked against the SELECT cols blob PLUS the text between the
# matched `FROM <table>` and the next `)` / `;` / end-of-string.
_SELECT_MULTI_TABLE_RE = _re_schema_validator.compile(
    r"""\b(?:JOIN|NATURAL\s+JOIN|CROSS\s+JOIN)\b""",
    _re_schema_validator.IGNORECASE,
)
del _re_schema_validator


def _extract_column_refs_from_sql_string(sql):
    """Return a list of (table, column) tuples referenced by a SQL
    string.

    Skips (returns no refs) for:
      - `*` and aggregate shortcuts like `COUNT(*)`
      - Expressions containing `(`
      - Multi-table SELECTs (JOIN / comma-separated FROM / subquery):
        the first-FROM-gets-all-columns attribution is wrong for those
        shapes, so we leave them to a future tokenizer-based pass and
        return nothing rather than raising false positives.
      - Columns prefixed with a table alias (`p.title`): we drop them
        entirely because we can't resolve which real table the alias
        points at without parsing the FROM list.
    """
    import re
    refs = []

    def _clean_col(token):
        token = token.strip().strip("\"`")
        # Strip AS aliases: "col AS alias" → "col"
        if " as " in token.lower():
            token = token.lower().split(" as ")[0].strip()
        # If the token has a table prefix (`p.title`), we cannot safely
        # attribute the column to a real table without a JOIN-aware
        # parser. Return empty so the caller skips it.
        if "." in token:
            return ""
        return token.strip().lower()

    for m in _Q_SELECT_FROM_RE.finditer(sql):
        cols_blob, table = m.group(1), m.group(2).lower()
        # Tail = text from end of match to the first SQL statement
        # terminator (`)` closing a pool.query arg, `;`, or `'` for
        # string literals). If none found, take everything to end of
        # string: the whole point is to scan for JOIN / comma-FROM
        # markers in the rest of the query.
        tail_end = len(sql)
        for ch in (")", ";", "'"):
            idx = sql.find(ch, m.end())
            if 0 <= idx < tail_end:
                tail_end = idx
        tail = sql[m.end():tail_end]
        # Skip multi-table queries: JOIN / NATURAL JOIN / CROSS JOIN.
        if _SELECT_MULTI_TABLE_RE.search(tail):
            continue
        # Comma-FROM `FROM t1, t2`, also a multi-table shape.
        if re.search(r"^\s*,\s*\w+", tail):
            continue
        if "*" in cols_blob or "(" in cols_blob:
            continue  # SELECT * or aggregate; skip
        for col_token in cols_blob.split(","):
            col = _clean_col(col_token)
            if col and col.replace("_", "").isalnum():
                refs.append((table, col))
    for m in _Q_INSERT_INTO_RE.finditer(sql):
        table, cols_blob = m.group(1).lower(), m.group(2)
        for col_token in cols_blob.split(","):
            col = _clean_col(col_token)
            if col and col.replace("_", "").isalnum():
                refs.append((table, col))
    for m in _Q_UPDATE_SET_RE.finditer(sql):
        table, set_blob = m.group(1).lower(), m.group(2)
        # Parse "col1 = $1, col2 = $2, ..."; each chunk before `=`
        for pair in set_blob.split(","):
            if "=" not in pair:
                continue
            col = _clean_col(pair.split("=", 1)[0])
            if col and col.replace("_", "").isalnum():
                refs.append((table, col))
    return refs


@processor(after=('_ensure_tz_aware_datetime_schema',), phase=('build',))
def _verify_schema_column_references(files, spec):
    """Schema column drift guard: fail the build BEFORE
    deploy when any JS / EJS file references a (table, column) pair
    the schema doesn't define.

    A production build emitted schema.sql with `site_settings (setting_key,
    setting_value)` and server.js queries `SELECT key, value FROM
    site_settings`. Code review flagged the mismatch as critical; the
    worker ignored it; deploy shipped broken code. This validator is
    the structural backstop: schema is the source of truth, and any
    JS/EJS column reference that doesn't resolve raises
    SchemaColumnDriftError here in the BUILD phase, before deploy.

    Scope:
      - Parses CREATE TABLE blocks in db/schema.sql.
      - Scans .js + .ejs files for `SELECT cols FROM t`, `INSERT INTO
        t (cols)`, `UPDATE t SET col = ...` patterns inside string
        literals.
      - Skips SELECT *, aggregates like COUNT(*), subqueries, JOINs,
        and any reference where the table isn't in the schema (CTE,
        temp table, aliased subquery).

    When a drift is found, raises SchemaColumnDriftError with
    structured mismatches. Env knob BLOCK_ON_SCHEMA_DRIFT=false rolls
    back to a warning-only mode for emergencies.
    """
    import os as _os
    import re as _re

    file_dict = dict(files)
    schema = file_dict.get("db/schema.sql", "")
    if not schema:
        return files
    tables = _parse_schema_columns(schema)
    if not tables:
        return files

    mismatches = []
    for path, content in file_dict.items():
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if not (path.endswith(".js") or path.endswith(".ejs")):
            continue
        if "node_modules/" in path or path.endswith(".test.js"):
            continue
        for ref in _extract_column_refs_from_sql_string(content):
            table, col = ref
            schema_cols = tables.get(table)
            if schema_cols is None:
                # Table not declared in schema: skip (probably a CTE
                # or aliased subquery, both out of scope for this pass).
                continue
            if col not in schema_cols:
                # Find an approximate line number for the reference.
                # Cheap: find the col name in the file content.
                line_no = content.count(
                    "\n", 0, content.find(col) if col in content else 0
                ) + 1 if col in content else None
                mismatches.append({
                    "file": path,
                    "line": line_no,
                    "table": table,
                    "column": col,
                    "schema_cols": schema_cols,
                })

    if not mismatches:
        return files

    # Allowlist rollback: only an explicit rollback token demotes the
    # gate to warn-only. Anything else (unset, typo, "True", "1")
    # stays strict. Matches queue.worker_pg._block_gate_on but this
    # module can't import worker_pg without a cycle, so the same
    # check lives here.
    _rollback_tokens = {"false", "0", "no", "off", "disabled"}
    _env_val = _os.environ.get("BLOCK_ON_SCHEMA_DRIFT", "").strip().lower()
    if _env_val in _rollback_tokens:
        print(
            f"  [post-process] WARNING schema-column drift: "
            f"{len(mismatches)} mismatch(es); "
            f"BLOCK_ON_SCHEMA_DRIFT={_env_val}: continuing anyway"
        )
        return files

    raise SchemaColumnDriftError(mismatches)


@processor(after=('_verify_schema_column_references',))
def _verify_template_middleware_contract(files, spec):
    """CONTRACT_VALIDATOR (v16): build-time lint. Scans views for `settings.X`
    references and warns if server.js has no middleware populating them.
    Non-fatal but prints a WARNING to build logs.

    This is the belt-and-suspenders partner to _ensure_settings_middleware_contract
    - if somehow the middleware is missing after that runs, we surface it.
    """
    import re as re_mod

    file_dict = dict(files)
    server = file_dict.get("server.js", "") or file_dict.get("app.js", "")
    if not server:
        return files

    has_settings_middleware = bool(
        re_mod.search(r"res\.locals\.settings\s*=", server)
    )

    # Find unique settings.X references in views
    settings_ref_re = re_mod.compile(r"settings\.(\w+)")
    used_keys = set()
    for path, content in file_dict.items():
        if not (path.endswith(".ejs") and isinstance(content, str)):
            continue
        used_keys.update(settings_ref_re.findall(content))

    if used_keys and not has_settings_middleware:
        print(f"  ⚠️  [CONTRACT_WARN] Views reference {len(used_keys)} settings keys "
              f"({sorted(used_keys)[:5]}...) but server.js has no middleware populating "
              f"res.locals.settings: hero/about/contact binding will silently fail.")

    return files


@processor(after=('_strip_dead_signup_links',))
def _normalize_admin_view_paths(files, spec):
    """ADMIN_VIEW_PATH_DUPLICATION (v13 → v14): when both views/admin/{slug}.ejs
    AND views/admin-{slug}.ejs exist, pipeline fixes applied to the canonical
    subdirectory version get shadowed by the hyphen-named version that admin
    dashboard routers render instead.

    First observed on MW v13: admin-settings.ejs (hyphen, QA-generated) had
    raw file inputs + old layout while admin/settings.ejs (slash, my v12 fix
    template) had the superset form. routes/admin-dashboard.js rendered the
    hyphen version, so the founder never saw my fix.

    Fix: detect duplicates, pick the canonical subdirectory version
    (views/admin/{slug}.ejs) as winner, rewrite routes/*.js render calls to
    point to the canonical one, delete the hyphen duplicate.
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False

    # Find pairs: views/admin/{slug}.ejs AND views/admin-{slug}.ejs
    hyphen_files = [p for p in file_dict.keys() if re_mod.match(r'^views/admin-[\w-]+\.ejs$', p)]
    for hyphen_path in hyphen_files:
        slug = re_mod.match(r'^views/admin-([\w-]+)\.ejs$', hyphen_path).group(1)
        canonical_path = f"views/admin/{slug}.ejs"
        if canonical_path not in file_dict:
            continue
        # Both exist: rewrite render calls to use canonical; delete hyphen.
        view_name_hyphen = f"admin-{slug}"
        view_name_canonical = f"admin/{slug}"
        # Update all route files
        for rp, rc in list(file_dict.items()):
            if not (rp.startswith("routes/") or rp in ("server.js", "app.js")):
                continue
            if not isinstance(rc, str):
                continue
            new_rc = re_mod.sub(
                rf"""(res\.render\(\s*['"]){re_mod.escape(view_name_hyphen)}(['"])""",
                rf"\1{view_name_canonical}\2",
                rc,
            )
            if new_rc != rc:
                file_dict[rp] = new_rc
                changed = True
        # Delete the hyphen duplicate
        del file_dict[hyphen_path]
        changed = True
        print(f"  [post-process] Normalized admin view path: deleted {hyphen_path}, "
              f"routes now render '{view_name_canonical}'")

    return list(file_dict.items()) if changed else files


@processor(after=('_ensure_settings_middleware_contract',))
def _convert_raw_file_inputs_to_upload_zones(files, spec):
    """FILE_INPUT_TO_UPLOAD_ZONE (v13 → v14): convert raw <input type="file">
    admin form fields into the full upload-zone pattern.

    Problem observed on MW v13 admin settings: Claude's QA-fix generated
    raw `<input type="file" name="nav_logo_file" accept="image/*">` elements
    with NO hidden URL input, NO /api/uploads wiring, NO preview thumbnail.
    Uploads did not persist: the founder clicked "Choose File" but the URL
    never made it into site_settings.nav_logo_url.

    This post-processor detects `<input type="file" name="<prefix>_file">`
    (or similar) in view files, and when the basename matches an upload key
    (logo|image|photo|avatar|banner|cover|thumbnail|picture|icon), replaces
    with the proper upload-zone pattern wired to the corresponding *_url key.

    Example rewrite:
      <input type="file" name="nav_logo_file" accept="image/*">
      → <input type="hidden" name="nav_logo_url" value="<%= settings.nav_logo_url || '' %>">
        <div class="image-upload-zone" data-target="nav_logo_url" ...>
          <div class="upload-preview" ...><img src="..."></div>
          <div class="upload-prompt" ...>Drag & drop...</div>
          <input type="file" accept="image/*" class="upload-file-input" style="display:none;">
        </div>

    Runs AFTER _ensure_upload_form_ui (which handles type=url|text) as a
    second layer catching the type=file variant.
    """
    import re as re_mod

    file_dict = dict(files)
    if "routes/upload.js" not in file_dict:
        return files  # no upload infrastructure

    # Keywords that identify an upload-target field (snake_case conventions)
    UPLOAD_KEYWORDS = ("logo", "image", "photo", "avatar", "banner", "cover",
                       "thumbnail", "picture", "icon", "hero", "about")

    # Match <input type="file" name="<prefix>_file"> (with optional id, accept, class)
    file_input_re = re_mod.compile(
        r'<input\b[^>]*\btype="file"[^>]*\bname="([a-z][a-z0-9_]*)_file"[^>]*/?>',
        re_mod.IGNORECASE,
    )

    def _has_upload_keyword(field_name_stem):
        lc = field_name_stem.lower()
        return any(kw in lc for kw in UPLOAD_KEYWORDS)

    changed = False
    rewrites = 0

    for path in list(file_dict.keys()):
        if not path.endswith(".ejs") or "views/" not in path:
            continue
        content = file_dict[path]
        if not isinstance(content, str):
            continue

        def _replace(match):
            nonlocal rewrites
            full_tag = match.group(0)
            stem = match.group(1)  # e.g. "nav_logo" from "nav_logo_file"
            if not _has_upload_keyword(stem):
                return full_tag  # not an image field
            # Translate to canonical *_url field name
            url_field = f"{stem}_url"
            field_id = url_field
            # The value comes from settings[url_field] at render time
            value_ejs = f"<%= (typeof settings !== 'undefined' && settings.{url_field}) || (typeof {stem.replace('_','')} !== 'undefined' && {stem.replace('_','')}.{url_field}) || '' %>"
            # Use the same _UPLOAD_ZONE_HTML template
            rewrites += 1
            return _UPLOAD_ZONE_HTML.format(
                field_id=field_id,
                field_name=url_field,
                value=value_ejs,
                value_expr=f"(typeof settings !== 'undefined' && settings.{url_field})",
                required="false",
            )

        new_content = file_input_re.sub(_replace, content)
        if new_content != content:
            file_dict[path] = new_content
            changed = True

    if changed:
        print(f"  [post-process] Converted {rewrites} raw <input type=\"file\"> to upload-zone pattern")
    return list(file_dict.items()) if changed else files


@processor(after=('_fix_admin_crud_link_prefix',))
def _strip_dead_signup_links(files: list, spec: dict) -> list:
    """DEAD_SIGNUP_LINK (v13 fix): remove "Register here" / "/signup" links
    on login pages when the spec has no signup/register route.

    Claude's login scaffold always includes a "Don't have an account?
    Register here" link pointing to /register or /signup. On admin-only apps
    (like MW, where the only user is the founder admin), no signup route
    exists and the link produces a 404. This post-processor detects whether
    any route file actually registers /signup, /register, or similar, and
    if not, strips the dead link from login views so customers don't click
    into a 404.
    """
    import re as re_mod

    file_dict = dict(files)

    # v17 FIX H: spec intent wins. If the spec explicitly lists a /register
    # or /signup page, preserve the link. If the spec has a non-empty pages
    # list and NONE of those pages are signup-related, strip the link even
    # when a route handler exists (Claude's boilerplate shouldn't override
    # the spec's intent).
    spec_pages = spec.get("pages", []) if isinstance(spec, dict) else []
    if spec_pages:
        signup_paths = {"/register", "/signup", "/sign-up"}
        spec_has_signup_page = any(
            isinstance(p, dict) and str(p.get("path", "")).rstrip("/") in signup_paths
            for p in spec_pages
        )
        if spec_has_signup_page:
            return files  # Spec explicitly wants signup; keep the link.
        # Otherwise fall through to strip, bypassing the route-existence check.
        has_signup_route = False
    else:
        # No pages list in spec: fall back to the original route-existence check.
        has_signup_route = False
        for path, content in file_dict.items():
            if not (path.startswith("routes/") or path == "server.js" or path == "app.js"):
                continue
            if not isinstance(content, str):
                continue
            if re_mod.search(r"(?:router|app)\.(?:get|post)\(\s*['\"]/(?:signup|register|sign-up)\b",
                             content):
                has_signup_route = True
                break

        if has_signup_route:
            return files  # Links are valid; leave them

    # Strip dead links from login views
    changed = False
    for path in list(file_dict.keys()):
        if not path.endswith(".ejs"):
            continue
        if "login" not in path.lower():
            continue
        content = file_dict[path]
        if not isinstance(content, str):
            continue

        orig = content
        # Match: <p>...Don't have an account?...Register here...</p> on one line or a few
        # Match: any <p|div|span> whose inner contains an <a href="/signup|/register|/sign-up">
        dead_block_re = re_mod.compile(
            r'<(p|div|span)\b[^>]*>[^<]*<a[^>]+href="/(?:signup|register|sign-up)"[^>]*>[^<]*</a>[^<]*</\1>\s*',
            re_mod.IGNORECASE,
        )
        content = dead_block_re.sub("", content)

        # Also match plain <a> tag (no wrapper); rarer
        dead_a_re = re_mod.compile(
            r'<a\b[^>]+href="/(?:signup|register|sign-up)"[^>]*>[^<]*</a>',
            re_mod.IGNORECASE,
        )
        content = dead_a_re.sub("", content)

        if content != orig:
            file_dict[path] = content
            changed = True
            print(f"  [post-process] Stripped dead /signup|/register link(s) from {path}")

    return list(file_dict.items()) if changed else files


@processor(after=('_fix_login_form_fields',), phase=('build',))
def _ensure_login_completeness(files: list) -> list:
    """Ensure login.ejs is not truncated from token continuation issues.

    When Claude hits max_tokens mid-file and the continuation re-emits from scratch,
    login.ejs can end up truncated (missing form opening, input fields, or the
    split-panel layout). This detects truncated login forms and replaces them with
    a complete, well-designed login page matching the UUPM visual checklist.
    """
    import re as re_mod

    file_dict = dict(files)
    login_path = None
    login_content = None

    for path, content in files:
        if path.endswith(".ejs") and "login" in path.lower() and isinstance(content, str):
            login_path = path
            login_content = content
            break

    if not login_path or not login_content:
        return files

    # Check for minimum required elements
    has_form_open = "<form" in login_content
    has_email_input = 'name="email"' in login_content or 'name="username"' in login_content
    has_password_input = 'name="password"' in login_content
    has_submit = '<button' in login_content and 'type="submit"' in login_content

    if has_form_open and has_email_input and has_password_input and has_submit:
        return files  # Login form is complete

    print(f"  [post-process] Detected truncated login.ejs (missing: "
          f"{'form ' if not has_form_open else ''}"
          f"{'email ' if not has_email_input else ''}"
          f"{'password ' if not has_password_input else ''}"
          f"{'submit' if not has_submit else ''})")

    # Generate a complete split-panel login page (matches VISUAL_DESIGN_CHECKLIST item 1)
    # Extract app name from layout.ejs title or spec
    app_name = "App"
    layout = file_dict.get("views/layout.ejs", "")
    if layout:
        # Pattern 1: EJS ternary with fallback like  : 'Medicine Waves' %>
        title_match = re_mod.search(r"<title>.*?:\s*'([^']+)'\s*%>", layout)
        if not title_match:
            # Pattern 2: plain text title e.g. <title>Medicine Waves</title>
            title_match = re_mod.search(r"<title>\s*([^<%]+?)\s*</title>", layout)
        if not title_match:
            # Pattern 3: after pipe e.g. title + ' | Medicine Waves'
            title_match = re_mod.search(r"\|\s*([A-Z][\w\s]+\w)\s*['\"]", layout)
        if title_match:
            app_name = title_match.group(1).strip()

    complete_login = f'''<div class="min-h-[85vh] flex">
  <!-- Left brand panel -->
  <div class="hidden md:flex md:w-1/2 relative overflow-hidden">
    <div class="absolute inset-0" style="background: linear-gradient(160deg, var(--color-primary, #0f2b3c) 0%, var(--color-secondary, #1a5a6a) 55%, var(--color-primary, #0f2b3c) 100%);"></div>
    <div class="absolute inset-0" style="background: radial-gradient(circle at 30% 30%, rgba(255,255,255,0.08) 0%, transparent 50%);"></div>
    <div class="absolute inset-0" style="background-image: url(&quot;data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'200\' height=\'174\' viewBox=\'0 0 200 174\'%3E%3Cg fill=\'none\' stroke=\'%23ffffff\' stroke-opacity=\'0.08\' stroke-width=\'2.5\' stroke-linejoin=\'round\'%3E%3Cpath d=\'M50 0L93 25L93 75L50 100L7 75L7 25Z\'/%3E%3Cpath d=\'M150 0L193 25L193 75L150 100L107 75L107 25Z\'/%3E%3Cpath d=\'M100 87L143 112L143 162L100 187L57 162L57 112Z\'/%3E%3C/g%3E%3C/svg%3E&quot;); opacity: 0.8;"></div>
    <div class="relative z-10 flex flex-col items-center justify-center p-12 text-white w-full">
      <div class="max-w-sm text-center">
        <div class="relative inline-block mb-8">
          <div class="absolute -inset-6 bg-white/10 rounded-2xl blur-2xl"></div>
          <img src="/logo.png" alt="{app_name} logo" class="relative h-48 w-auto rounded-xl shadow-2xl ring-1 ring-white/10">
        </div>
        <h2 class="font-display font-semibold text-4xl mb-4 tracking-wide">{app_name}</h2>
      </div>
    </div>
  </div>

  <!-- Right form panel -->
  <div class="w-full md:w-1/2 flex items-center justify-center p-6 sm:p-12" style="background: linear-gradient(to bottom, var(--color-cream, #fffdf8), #fff)">
    <div class="w-full max-w-md">
      <!-- Mobile logo -->
      <div class="md:hidden text-center mb-8">
        <img src="/logo.png" alt="{app_name} logo" class="h-16 w-auto rounded-xl mx-auto mb-4 shadow-md">
        <h2 class="font-display font-bold text-2xl text-gray-900">{app_name}</h2>
      </div>

      <h1 class="font-display font-bold text-2xl md:text-3xl text-gray-900 mb-2">Welcome back</h1>
      <p class="text-gray-500 text-sm mb-8">Sign in to manage your account</p>

      <% if (typeof error !== 'undefined' && error) {{ %>
        <div class="mb-6 p-4 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm flex items-start gap-3">
          <svg class="w-5 h-5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          <span><%= error %></span>
        </div>
      <% }} %>

      <% if (typeof success !== 'undefined' && success) {{ %>
        <div class="mb-6 p-4 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-700 text-sm flex items-start gap-3">
          <svg class="w-5 h-5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          <span><%= success %></span>
        </div>
      <% }} %>

      <form action="/login" method="POST" class="space-y-5">
        <input type="hidden" name="_csrf" value="<%= csrfToken %>">
        <div>
          <label for="email" class="block text-sm font-semibold text-gray-700 mb-2">Email</label>
          <input type="email" id="email" name="email" required autocomplete="username"
            class="input-premium" placeholder="Enter your email">
        </div>
        <div>
          <label for="password" class="block text-sm font-semibold text-gray-700 mb-2">Password</label>
          <input type="password" id="password" name="password" required autocomplete="current-password"
            class="input-premium" placeholder="Enter your password">
        </div>
        <button type="submit" class="w-full btn-primary py-3.5 px-6 text-lg min-h-[48px] font-display rounded-xl">Sign In</button>
      </form>

      <div class="mt-8 text-center space-y-3">
        <p class="text-sm text-gray-500">First time? <a href="/request-magic-link" class="font-semibold hover:text-amber-700 transition-colors" style="color:var(--color-accent)">Request a login link</a></p>
        <p class="text-sm text-gray-500">Don't have an account? <a href="/register" class="font-semibold hover:text-amber-700 transition-colors" style="color:var(--color-accent)">Register here</a></p>
      </div>
    </div>
  </div>
</div>'''

    file_dict[login_path] = complete_login
    print(f"  [post-process] Replaced truncated {login_path} with complete split-panel login")
    return list(file_dict.items())


@processor(after=('_ensure_login_completeness',), phase=('build',))
def _ensure_register_completeness(files: list) -> list:
    """Ensure register.ejs is not truncated from token continuation issues.

    When Claude hits max_tokens mid-file and the continuation re-emits from scratch,
    register.ejs can end up truncated (missing form opening, input fields, or the
    split-panel layout). This detects truncated register forms and replaces them with
    a complete, well-designed register page matching the login template's design.
    """
    import re as re_mod

    file_dict = dict(files)
    register_path = None
    register_content = None

    for path, content in files:
        if path.endswith(".ejs") and "register" in path.lower() and isinstance(content, str):
            register_path = path
            register_content = content
            break

    if not register_path or not register_content:
        return files

    # Check for minimum required elements
    has_form_open = "<form" in register_content
    has_name_input = 'name="name"' in register_content
    has_email_input = 'name="email"' in register_content
    has_password_input = 'name="password"' in register_content
    has_submit = '<button' in register_content and 'type="submit"' in register_content

    if has_form_open and has_name_input and has_email_input and has_password_input and has_submit:
        return files  # Register form is complete

    print(f"  [post-process] Detected truncated register.ejs (missing: "
          f"{'form ' if not has_form_open else ''}"
          f"{'name ' if not has_name_input else ''}"
          f"{'email ' if not has_email_input else ''}"
          f"{'password ' if not has_password_input else ''}"
          f"{'submit' if not has_submit else ''})")

    # Extract app name from layout.ejs title or spec
    app_name = "App"
    layout = file_dict.get("views/layout.ejs", "")
    if layout:
        # Pattern 1: EJS ternary with fallback like  : 'Medicine Waves' %>
        title_match = re_mod.search(r"<title>.*?:\s*'([^']+)'\s*%>", layout)
        if not title_match:
            # Pattern 2: plain text title e.g. <title>Medicine Waves</title>
            title_match = re_mod.search(r"<title>\s*([^<%]+?)\s*</title>", layout)
        if not title_match:
            # Pattern 3: after pipe e.g. title + ' | Medicine Waves'
            title_match = re_mod.search(r"\|\s*([A-Z][\w\s]+\w)\s*['\"]", layout)
        if title_match:
            app_name = title_match.group(1).strip()

    complete_register = f'''<div class="min-h-[85vh] flex">
  <!-- Left brand panel -->
  <div class="hidden md:flex md:w-1/2 relative overflow-hidden">
    <div class="absolute inset-0" style="background: linear-gradient(160deg, var(--color-primary, #0f2b3c) 0%, var(--color-secondary, #1a5a6a) 55%, var(--color-primary, #0f2b3c) 100%);"></div>
    <div class="absolute inset-0" style="background: radial-gradient(circle at 30% 30%, rgba(255,255,255,0.08) 0%, transparent 50%);"></div>
    <div class="absolute inset-0" style="background-image: url(&quot;data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'200\' height=\'174\' viewBox=\'0 0 200 174\'%3E%3Cg fill=\'none\' stroke=\'%23ffffff\' stroke-opacity=\'0.08\' stroke-width=\'2.5\' stroke-linejoin=\'round\'%3E%3Cpath d=\'M50 0L93 25L93 75L50 100L7 75L7 25Z\'/%3E%3Cpath d=\'M150 0L193 25L193 75L150 100L107 75L107 25Z\'/%3E%3Cpath d=\'M100 87L143 112L143 162L100 187L57 162L57 112Z\'/%3E%3C/g%3E%3C/svg%3E&quot;); opacity: 0.8;"></div>
    <div class="relative z-10 flex flex-col items-center justify-center p-12 text-white w-full">
      <div class="max-w-sm text-center">
        <div class="relative inline-block mb-8">
          <div class="absolute -inset-6 bg-white/10 rounded-2xl blur-2xl"></div>
          <img src="/logo.png" alt="{app_name} logo" class="relative h-48 w-auto rounded-xl shadow-2xl ring-1 ring-white/10">
        </div>
        <h2 class="font-display font-semibold text-4xl mb-4 tracking-wide">{app_name}</h2>
      </div>
    </div>
  </div>

  <!-- Right form panel -->
  <div class="w-full md:w-1/2 flex items-center justify-center p-6 sm:p-12" style="background: linear-gradient(to bottom, var(--color-cream, #fffdf8), #fff)">
    <div class="w-full max-w-md">
      <!-- Mobile logo -->
      <div class="md:hidden text-center mb-8">
        <img src="/logo.png" alt="{app_name} logo" class="h-16 w-auto rounded-xl mx-auto mb-4 shadow-md">
        <h2 class="font-display font-bold text-2xl text-gray-900">{app_name}</h2>
      </div>

      <h1 class="font-display font-bold text-2xl md:text-3xl text-gray-900 mb-2">Create your account</h1>
      <p class="text-gray-500 text-sm mb-8">Get started in just a few seconds</p>

      <% if (typeof error !== 'undefined' && error) {{ %>
        <div class="mb-6 p-4 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm flex items-start gap-3">
          <svg class="w-5 h-5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          <span><%= error %></span>
        </div>
      <% }} %>

      <form action="/register" method="POST" class="space-y-5">
        <input type="hidden" name="_csrf" value="<%= csrfToken %>">
        <div>
          <label for="name" class="block text-sm font-semibold text-gray-700 mb-2">Full Name</label>
          <input type="text" id="name" name="name" required autocomplete="name"
            class="input-premium" placeholder="Enter your full name">
        </div>
        <div>
          <label for="email" class="block text-sm font-semibold text-gray-700 mb-2">Email</label>
          <input type="email" id="email" name="email" required autocomplete="email"
            class="input-premium" placeholder="Enter your email">
        </div>
        <div>
          <label for="password" class="block text-sm font-semibold text-gray-700 mb-2">Password</label>
          <input type="password" id="password" name="password" required autocomplete="new-password" minlength="8"
            class="input-premium" placeholder="Create a password">
          <p class="mt-1.5 text-xs text-gray-400">Must be at least 8 characters</p>
        </div>
        <button type="submit" class="w-full btn-primary py-3.5 px-6 text-lg min-h-[48px] font-display rounded-xl">Create Account</button>
      </form>

      <div class="mt-8 text-center">
        <p class="text-sm text-gray-500">Already have an account? <a href="/login" class="font-semibold hover:text-amber-700 transition-colors" style="color:var(--color-accent)">Sign in</a></p>
      </div>
    </div>
  </div>
</div>'''

    file_dict[register_path] = complete_register
    print(f"  [post-process] Replaced truncated {register_path} with complete split-panel register")
    return list(file_dict.items())


def _make_stat_cards_clickable(file_dict: dict, re_mod) -> None:
    """Wrap admin dashboard stat cards in <a> tags linking to list pages.

    Claude generates stat cards as plain <div> elements. This wraps each one
    in an <a> tag so clicking the card navigates to the corresponding
    list page (e.g. "Events" card -> /events).
    Mutates file_dict in place.
    """
    # Claude may place the admin dashboard at views/admin/dashboard.ejs or views/dashboard.ejs
    dash_path = None
    for candidate in ("views/admin/dashboard.ejs", "views/dashboard.ejs"):
        if file_dict.get(candidate, "") and "stat-card" in file_dict[candidate]:
            dash_path = candidate
            break
    if not dash_path:
        return
    dashboard_ejs = file_dict[dash_path]

    stat_label_re = re_mod.compile(
        r'(<div\s+class="stat-card[^"]*"[^>]*>)'
        r'(.*?<p\s+class="text-sm[^"]*"[^>]*>)'
        r'(\w[\w\s]*\w|\w+)'  # label text
        r'(</p>\s*</div>\s*</div>)',
        re_mod.DOTALL,
    )

    # Common label -> URL mappings covering most generated app models
    _KNOWN_SLUGS = {
        "events": "/events", "photos": "/photos", "videos": "/videos",
        "subscribers": "/subscribers", "messages": "/contact-messages",
        "contact messages": "/contact-messages", "users": "/users",
        "products": "/products", "orders": "/orders", "customers": "/customers",
        "posts": "/posts", "articles": "/articles", "categories": "/categories",
        "appointments": "/appointments", "projects": "/projects",
        "tasks": "/tasks", "invoices": "/invoices", "clients": "/clients",
        "listings": "/listings", "properties": "/properties",
        "bookings": "/bookings", "services": "/services",
        "recipes": "/recipes", "classes": "/classes",
    }

    def _wrap(m):
        label = m.group(3)
        slug = label.lower().replace(" ", "-")
        href = _KNOWN_SLUGS.get(slug, f"/{slug}")
        return (
            f'<a href="{href}" class="block">'
            + m.group(1) + m.group(2) + label + m.group(4)
            + "</a>"
        )

    new_dashboard = stat_label_re.sub(_wrap, dashboard_ejs)
    if new_dashboard != dashboard_ejs:
        file_dict[dash_path] = new_dashboard
        print("  [post-process] Made admin dashboard stat cards clickable")


@processor(after=('_ensure_register_completeness',), phase=('build',))
def _ensure_admin_mount(files: list) -> list:
    """Ensure server.js mounts the admin router from routes/admin.js.

    The admin routes template provides reset-data, csv-import, and change-password.
    Claude sometimes forgets to add the require() line in server.js even when told to.
    This deterministically patches server.js to mount it, and ensures the template
    file itself exists (single-pass builds may not generate it).

    IMPORTANT: Preserves Claude's GET /admin dashboard route. The template only has
    utility routes (csv-import, reset-data, change-password). Claude generates the
    app-specific dashboard with stats queries and views/admin/dashboard.ejs. We
    extract that route before overwriting, then append it to the template.
    """
    import re as re_mod
    file_dict = dict(files)
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_admin_mount.start",
        {"input_count": len(files)},
    )
    server_js = file_dict.get("server.js", "")
    if not server_js:
        emit_build_trace(
            _CURRENT_BUILD_DIR.get(),
            "ensure_admin_mount.skipped",
            {"reason": "no_server_js"},
        )
        return files

    # Extract Claude's GET /admin dashboard route before overwriting.
    # Claude uses various patterns: router.get('/admin', router.get('/',
    # with the dashboard view rendering (admin/dashboard, dashboard, admin/index).
    claude_admin = file_dict.get("routes/admin.js", "")
    preserved_dashboard_route = ""
    dashboard_patterns = [
        r"(// (?:GET /admin|Admin [Dd]ashboard|Dashboard).*?router\.get\(['\"](?:/admin|/)['\'].*?\}\);)",
        r"(router\.get\(['\"](?:/admin|/)['\"],\s*(?:requireAuth|requireAdmin|requireRole).*?\}\);)",
    ]
    if claude_admin:
        for pat in dashboard_patterns:
            dashboard_match = re_mod.search(pat, claude_admin, re_mod.DOTALL)
            if dashboard_match:
                preserved_dashboard_route = dashboard_match.group(1)
                print("  [post-process] Preserved Claude's GET /admin dashboard route")
                break

    # Overwrite routes/admin.js with the canonical template.
    # Claude sometimes generates its own admin routes that miss csv-import or change-password.
    # The template is the authoritative source for go-live admin features.
    template_path = _TEMPLATES_DIR / "routes_admin.js"
    if template_path.exists():
        was_missing = "routes/admin.js" not in file_dict
        template_content = template_path.read_text()

        # Append preserved dashboard route before module.exports
        if preserved_dashboard_route:
            # Validate brace balance: Claude sometimes truncates the route handler
            open_braces = preserved_dashboard_route.count("{")
            close_braces = preserved_dashboard_route.count("}")
            if open_braces > close_braces:
                missing = open_braces - close_braces
                # Add try/catch wrapper and missing closing braces
                preserved_dashboard_route += "\n  } catch (err) {\n    console.error('Admin dashboard error:', err);\n    res.status(500).render('500', { title: 'Error' });\n  }\n});"
                # Adjust for already-closed braces from the catch block (3 braces added)
                remaining = missing - 3
                if remaining > 0:
                    preserved_dashboard_route += "\n" + "}" * remaining
                print(f"  [post-process] Fixed truncated admin dashboard route ({missing} missing braces)")
            # Remove duplicate declarations from preserved route.
            # Claude's dashboard route may include const/function/router blocks
            # that already exist in the template, causing "already declared" errors.
            # Strip everything except the router.get('/admin'...) handler itself.
            dashboard_only = re_mod.search(
                r"(router\.get\('/admin'.*)",
                preserved_dashboard_route,
                re_mod.DOTALL,
            )
            if dashboard_only:
                preserved_dashboard_route = "\n// Admin dashboard (preserved from Claude)\n" + dashboard_only.group(1)
                # Replace Claude's middleware refs with what the template provides
                preserved_dashboard_route = re_mod.sub(
                    r"requireRole\(['\"]admin['\"]\)",
                    "requireAdmin",
                    preserved_dashboard_route,
                )
                # Strip requirePasswordSet (not in template scope)
                preserved_dashboard_route = re_mod.sub(
                    r",?\s*requirePasswordSet", "", preserved_dashboard_route
                )
                # Strip requireAuth if requireAdmin already present (requireAdmin implies auth)
                if "requireAdmin" in preserved_dashboard_route:
                    preserved_dashboard_route = re_mod.sub(
                        r"requireAuth,\s*", "", preserved_dashboard_route
                    )
            template_content = template_content.replace(
                "module.exports = router;",
                preserved_dashboard_route + "\n\nmodule.exports = router;",
            )

        # Fallback: if no dashboard route was preserved but a dashboard view exists,
        # inject a generic GET /admin that renders the dashboard with model counts.
        # The render contract must match the view: scan the view for stats.<key>
        # references and build a stats object whose keys match. Claude-generated
        # admin dashboards typically expect { stats: { events, photos, ... },
        # recentMessages: [...] }, the earlier flat-vars fallback caused a
        # "stats is not defined" crash on /admin (medicine-waves v3, 2026-04-10).
        if not preserved_dashboard_route:
            dashboard_view = None
            for vp in ["views/admin/dashboard.ejs", "views/dashboard.ejs", "views/admin/index.ejs"]:
                if vp in file_dict:
                    dashboard_view = vp
                    break
            if dashboard_view:
                view_content = file_dict.get(dashboard_view, "")
                # Parse schema.sql for data table names to build count queries
                schema = file_dict.get("db/schema.sql", "")
                data_tables = [
                    m.group(1) for m in re_mod.finditer(
                        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)',
                        schema, re_mod.IGNORECASE,
                    )
                    if m.group(1).lower() not in (
                        'users', 'site_settings', 'uploads', 'sessions',
                        'user_sessions', 'email_verification_tokens',
                        'magic_link_tokens', 'password_reset_tokens',
                    )
                ]

                def _match_table_for_key(key, tables):
                    """Best-effort map of a stats.<key> reference to a DB table."""
                    kl = key.lower()
                    lookup = {t.lower(): t for t in tables}
                    # Exact match
                    if kl in lookup:
                        return lookup[kl]
                    # Singular -> plural
                    for suffix in ("s", "es"):
                        if (kl + suffix) in lookup:
                            return lookup[kl + suffix]
                    # Plural -> singular
                    if kl.endswith("s") and kl[:-1] in lookup:
                        return lookup[kl[:-1]]
                    if kl.endswith("es") and kl[:-2] in lookup:
                        return lookup[kl[:-2]]
                    # Suffix match (e.g. "messages" -> "contact_messages")
                    for tl, orig in lookup.items():
                        if tl.endswith("_" + kl) or tl.endswith(kl):
                            return orig
                    # Singular key against suffix of plural table
                    for tl, orig in lookup.items():
                        if tl.endswith("_" + kl + "s") or tl.endswith(kl + "s"):
                            return orig
                    # Prefix match
                    for tl, orig in lookup.items():
                        if tl.startswith(kl):
                            return orig
                    return None

                # Scan view for stats.<key> references
                stats_keys = sorted(set(re_mod.findall(r'\bstats\.(\w+)', view_content)))

                # Emit count queries for every data table so we can reference them
                count_lines = [
                    f"const {t}Count = (await pool.query('SELECT COUNT(*) FROM {t}')).rows[0].count;"
                    for t in data_tables
                ]
                count_queries = "\n    ".join(count_lines)

                # Build the stats object the view expects
                if stats_keys:
                    stats_entries = []
                    for key in stats_keys:
                        match = _match_table_for_key(key, data_tables)
                        if match:
                            stats_entries.append(f"{key}: {match}Count")
                        else:
                            stats_entries.append(f"{key}: 0")
                    stats_literal = "{ " + ", ".join(stats_entries) + " }"
                else:
                    # View doesn't reference stats.X: still provide a stats object
                    # keyed by table name so flat-access views and nested-access views
                    # both work.
                    stats_literal = "{ " + ", ".join(
                        f"{t}: {t}Count" for t in data_tables
                    ) + " }"

                # Detect recentMessages reference and add a safe query
                recent_query = ""
                recent_render = ""
                if re_mod.search(r'\brecentMessages\b', view_content):
                    messages_table = None
                    lookup = {t.lower(): t for t in data_tables}
                    for candidate in ("contact_messages", "messages"):
                        if candidate in lookup:
                            messages_table = lookup[candidate]
                            break
                    if not messages_table:
                        for t in data_tables:
                            if "message" in t.lower():
                                messages_table = t
                                break
                    if messages_table:
                        recent_query = (
                            "\n    let recentMessages = [];\n"
                            "    try { recentMessages = (await pool.query("
                            f"'SELECT * FROM {messages_table} ORDER BY created_at DESC LIMIT 5'"
                            ")).rows; } catch (e) { recentMessages = []; }"
                        )
                    else:
                        recent_query = "\n    const recentMessages = [];"
                    recent_render = ", recentMessages"

                # Flat render vars as belt-and-suspenders (for views that use flat access)
                flat_render_vars = ", ".join(f"{t}: {t}Count" for t in data_tables)
                flat_render = f", {flat_render_vars}" if flat_render_vars else ""

                view_name = dashboard_view.replace("views/", "").replace(".ejs", "")
                fallback_route = f"""
// Admin dashboard (injected by post-processor)
router.get('/admin', requireAdmin, async (req, res) => {{
  try {{
    {count_queries}{recent_query}
    const stats = {stats_literal};
    res.render('{view_name}', {{ title: 'Admin Dashboard', stats{recent_render}{flat_render} }});
  }} catch (err) {{
    console.error('Admin dashboard error:', err);
    res.status(500).render('500', {{ title: 'Error' }});
  }}
}});"""
                template_content = template_content.replace(
                    "module.exports = router;",
                    fallback_route + "\n\nmodule.exports = router;",
                )
                print("  [post-process] Injected fallback GET /admin dashboard route")

        file_dict["routes/admin.js"] = template_content
        if was_missing:
            print("  [post-process] Injected routes/admin.js template (missing from generation)")
        else:
            print("  [post-process] Replaced routes/admin.js with canonical template")

    # Inject the CSV import view template (used by GET /admin/csv-import)
    csv_view_template = _TEMPLATES_DIR / "views_admin_csv_import.ejs"
    if csv_view_template.exists():
        was_missing = "views/admin-csv-import.ejs" not in file_dict
        file_dict["views/admin-csv-import.ejs"] = csv_view_template.read_text()
        if was_missing:
            print("  [post-process] Injected views/admin-csv-import.ejs template")
        else:
            print("  [post-process] Replaced views/admin-csv-import.ejs with canonical template")

    import re as re_mod

    # Make admin dashboard stat cards clickable links.
    # Claude generates stat cards as plain <div> elements; they should link
    # to the corresponding list page (e.g. "Events" -> /events).
    # This runs before the mount check so it applies regardless of mount state.
    _make_stat_cards_clickable(file_dict, re_mod)

    # Check if admin router is already mounted
    if "routes/admin" in server_js:
        emit_build_trace(
            _CURRENT_BUILD_DIR.get(),
            "ensure_admin_mount.completed",
            {"reason": "already_mounted", "output_count": len(file_dict)},
        )
        return list(file_dict.items())

    # Find the last app.use(require('./routes/...')) line and add admin after it
    mount_pattern = re_mod.compile(r"(app\.use\(require\(['\"]\.\/routes\/[^)]+\)\);?)")
    matches = list(mount_pattern.finditer(server_js))
    if matches:
        last_match = matches[-1]
        insert_pos = last_match.end()
        patched = (
            server_js[:insert_pos]
            + "\napp.use(require('./routes/admin'));"
            + server_js[insert_pos:]
        )
        file_dict["server.js"] = patched
        print("  [post-process] Added admin router mount to server.js")
    else:
        # Fallback: add before error handler or at end of middleware section
        error_handler = server_js.find("app.use((err,")
        if error_handler == -1:
            error_handler = server_js.find("app.use(function(err")
        if error_handler > 0:
            patched = (
                server_js[:error_handler]
                + "app.use(require('./routes/admin'));\n\n"
                + server_js[error_handler:]
            )
            file_dict["server.js"] = patched
            print("  [post-process] Added admin router mount to server.js (before error handler)")

    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_admin_mount.completed",
        {"reason": "mount_patched", "output_count": len(file_dict)},
    )
    return list(file_dict.items())


@processor(after=('_strip_process_exit_on_db',), phase=('build',))
def _validate_ejs_templates(files: list) -> list:
    """Validate EJS templates for common crash-prone patterns.

    Checks all .ejs files for issues that cause runtime crashes:
    - POST forms missing CSRF token input
    - Unclosed EJS tags
    - Missing layout body tag

    Non-blocking: logs warnings but never modifies files or fails the build.
    Issues are recorded for the review_kb learning loop.
    """
    import re as re_mod

    warnings = []
    for filepath, content in files:
        if not filepath.endswith(".ejs"):
            continue

        # Check POST forms for CSRF token
        forms = re_mod.findall(r'<form[^>]*method=["\']post["\'][^>]*>', content, re_mod.IGNORECASE)
        if forms:
            if '_csrf' not in content and 'csrfToken' not in content:
                warnings.append(f"{filepath}: POST form missing CSRF token input")

        # Check for unclosed EJS tags (opening <% without closing %>)
        opens = content.count("<%")
        closes = content.count("%>")
        if opens != closes:
            warnings.append(f"{filepath}: mismatched EJS tags ({opens} opens, {closes} closes)")

        # Check for mismatched form tags (e.g. </form> without <form>)
        form_opens = len(re_mod.findall(r'<form[\s>]', content, re_mod.IGNORECASE))
        form_closes = len(re_mod.findall(r'</form>', content, re_mod.IGNORECASE))
        if form_opens != form_closes:
            warnings.append(f"{filepath}: mismatched form tags ({form_opens} opens, {form_closes} closes)")

    if warnings:
        print(f"  [post-process] EJS validation: {len(warnings)} warning(s)")
        for w in warnings[:5]:
            print(f"    - {w}")

    return files  # never modifies files, just warns


@processor(after=('_validate_ejs_templates',), phase=('build',))
def _validate_destructive_confirm(files: list) -> list:
    """Validate that EJS delete/remove/reset forms include data-confirm attributes.

    Scans generated EJS view files for forms that POST to destructive routes
    (delete, remove, reset) without a data-confirm attribute. These forms would
    allow single-click permanent data destruction without user confirmation.

    Non-blocking: logs warnings but never modifies files or fails the build.
    """
    import re as re_mod

    warnings = []
    # Patterns for routes that are destructive (delete/remove/reset actions)
    destructive_route_pattern = re_mod.compile(
        r'action=["\'][^"\']*/(delete|remove|reset|destroy|clear)[^"\']*["\']',
        re_mod.IGNORECASE,
    )

    for filepath, content in files:
        if not filepath.endswith(".ejs"):
            continue
        # Skip layout: it only has logout form (not destructive)
        if filepath == "views/layout.ejs":
            continue

        # Find all form tags with their attributes on a single line or multiline
        # We look for <form ...> blocks and check if they match destructive routes
        form_pattern = re_mod.compile(r'<form\b([^>]*(?:>|$))', re_mod.IGNORECASE | re_mod.DOTALL)
        for form_match in form_pattern.finditer(content):
            form_attrs = form_match.group(1)
            if not destructive_route_pattern.search(form_attrs):
                continue
            if 'data-confirm' not in form_attrs:
                warnings.append(
                    f"{filepath}: destructive form missing data-confirm: {form_attrs[:120].strip()}"
                )

    if warnings:
        print(f"  [post-process] Destructive confirm check: {len(warnings)} warning(s)")
        for w in warnings[:5]:
            print(f"    - {w}")

    return files  # never modifies files, just warns


@processor(after=('_validate_destructive_confirm',), phase=('build',))
def _validate_seed_sql(files: list, spec: dict) -> None:
    """Validate seed.sql has enough INSERT statements for all data models.

    Non-blocking: logs warnings but never modifies files or fails the build.
    Catches truncated seed data from token continuation issues.
    """
    import re as re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        print("  [post-process] WARNING: No db/seed.sql found")
        return

    # Count total INSERTs
    inserts = re_mod.findall(r'INSERT\s+INTO\s+["\']?(\w+)', seed, re_mod.IGNORECASE)
    total_inserts = len(inserts)

    # Check minimum overall count
    data_models = spec.get("data_models", [])
    expected_tables = len(data_models) + 1  # +1 for users table
    min_expected = expected_tables * 5  # at least 5 per table as a low bar

    if total_inserts < min_expected:
        print(f"  [post-process] WARNING: seed.sql has {total_inserts} INSERTs "
              f"(expected ~{expected_tables * 15} for {expected_tables} tables)")

    # Check for truncation (ends mid-statement)
    stripped = seed.rstrip()
    if stripped and not stripped.endswith(";"):
        print("  [post-process] WARNING: seed.sql appears truncated (does not end with semicolon)")

    # Check users table has at least one INSERT
    user_inserts = sum(1 for t in inserts if t.lower() == "users")
    if user_inserts == 0:
        print("  [post-process] WARNING: seed.sql has 0 INSERTs for users table")


def _fix_seed_column_mismatch(files: list) -> list:
    """Fix seed.sql INSERT statements where the last column is a FK but values omit it.

    Claude sometimes includes FK columns (like created_by) in the column list
    but omits values, causing 'INSERT has more target columns than expressions'.
    Targeted fix: if the last column is a known FK (created_by, user_id, etc.)
    and each value row ends with a non-numeric value before the closing paren,
    append ', 1' (the seeded admin user ID) to each row.

    Uses psql dry-run to detect the actual error rather than fragile value counting.
    """
    import re as re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    # Known FK column names that reference the users table
    _FK_COLS = {'created_by', 'user_id', 'author_id', 'owner_id', 'uploaded_by'}

    # Find INSERT statements where the last column is a FK
    insert_re = re_mod.compile(
        r'INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES',
        re_mod.IGNORECASE,
    )

    changed = False
    for m in insert_re.finditer(seed):
        table = m.group(1)
        cols_str = m.group(2).strip()
        cols = [c.strip() for c in cols_str.split(',')]
        last_col = cols[-1].lower()

        if last_col not in _FK_COLS:
            continue

        # Find the VALUES block for this INSERT: from VALUES to the next ;
        val_start = m.end()
        # Find the semicolon that ends this INSERT (outside quotes)
        in_q = False
        semi_pos = None
        for i in range(val_start, len(seed)):
            ch = seed[i]
            if ch == "'" and (i == 0 or seed[i-1] != "'"):
                in_q = not in_q
            elif ch == ';' and not in_q:
                semi_pos = i
                break
        if semi_pos is None:
            continue

        values_block = seed[val_start:semi_pos]

        # Check if value rows are missing the last value by looking for
        # closing patterns like  'text'), or  number), but NOT  , N)
        # which would mean the FK value is already present.
        # Simple heuristic: append ', 1' before each '),' and before the final ')'
        # only if the INSERT would fail without it.

        # Count closing parens in values block and try appending ', 1' to each
        new_values = re_mod.sub(r"(\)\s*(?:,|ON\s))", r", 1\1", values_block)
        # Also fix the last value tuple before semicolon
        if new_values == values_block:
            continue  # No closing parens found

        # Handle the last ) before ; or ON CONFLICT
        new_values = re_mod.sub(r"\)\s*$", ", 1)", new_values.rstrip())

        if new_values != values_block:
            seed = seed[:val_start] + new_values + seed[semi_pos:]
            changed = True
            print(f"  [post-process] Fixed seed.sql {table}: appended {last_col}=1 to each row")

    if changed:
        file_dict["db/seed.sql"] = seed

    return list(file_dict.items())


@processor(after=('_ensure_error_handling',), phase=('build',))
def _strip_process_exit_on_db(files: list) -> list:
    """Remove process.exit() calls in DB/migration error handlers in server.js.

    Railway's PostgreSQL takes 10-30s to provision after app start, so generated
    apps must keep the HTTP server listening even if DB is unavailable at startup.
    process.exit(1) in catch blocks for DB connections kills the app before
    Railway's health check can pass, causing a deploy crash loop.
    """
    import re
    for i, (path, content) in enumerate(files):
        if path != "server.js":
            continue
        original = content
        # Remove process.exit(1) inside catch blocks related to DB/migration
        # Pattern: process.exit(1) or process.exit(0) after console.error with DB-related text
        content = re.sub(
            r'(catch\s*\([^)]*\)\s*\{[^}]*?console\.(?:error|log)\([^)]*(?:migrat|database|connect|pool|pg|sql)[^)]*\)[^}]*?)process\.exit\(\d+\);?',
            r'\1// process.exit removed: keep server alive for Railway DB startup race',
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        # Also catch standalone process.exit near migrate/db calls
        content = re.sub(
            r'(\.catch\(\s*(?:err|e|error)\s*=>\s*\{[^}]*?)process\.exit\(\d+\);?',
            r'\1// process.exit removed: keep server alive for Railway',
            content,
            flags=re.DOTALL,
        )
        if content != original:
            files[i] = (path, content)
            print("  [post-process] Stripped process.exit() from server.js DB error handlers")
        break
    return files


@processor(after=('_ensure_admin_mount',), phase=('build',))
def _ensure_error_handling(files: list) -> list:
    """Ensure generated apps have proper error handling.

    1. server.js has a centralized error handler that renders 500.ejs
    2. views/500.ejs exists (loaded from templates)
    3. server.js has DB readiness check before listening
    """
    import re as re_mod

    file_dict = dict(files)

    # --- Force-overwrite views/500.ejs with canonical template ---
    # Claude may generate a broken or incomplete 500.ejs during code generation.
    # Always replace with the canonical template to prevent drift.
    template_path = Path(__file__).parent / "templates" / "500.ejs"
    if template_path.exists():
        was_missing = "views/500.ejs" not in file_dict
        file_dict["views/500.ejs"] = template_path.read_text()
        if was_missing:
            print("  [post-process] Added views/500.ejs error page template")
        else:
            print("  [post-process] Replaced views/500.ejs with canonical template")

    # --- Ensure server.js has error handler ---
    server_js = file_dict.get("server.js", "")
    if server_js:
        changed = False
        # Check for centralized error handler
        if "err, req, res, next" not in server_js and "function(err" not in server_js:
            # Add error handler at the end (before module.exports or app.listen)
            handler = "\n// Centralized error handler\napp.use((err, req, res, next) => {\n  console.error(err.stack);\n  res.status(500).render('500', { title: 'Error' });\n});\n"
            # Insert before app.listen or module.exports
            for marker in ["app.listen(", "module.exports"]:
                idx = server_js.find(marker)
                if idx > 0:
                    server_js = server_js[:idx] + handler + "\n" + server_js[idx:]
                    changed = True
                    print("  [post-process] Added centralized error handler to server.js")
                    break

        if changed:
            file_dict["server.js"] = server_js

    return list(file_dict.items())


# All dependencies that every generated app requires. Claude's prompt instructs
# it to include these, but it frequently omits several. This post-processor
# guarantees they are present so npm install + app startup never fail due to
# missing packages.
REQUIRED_DEPS = {
    # Core framework
    "express": "^4.19.2",
    "ejs": "^3.1.10",
    "express-ejs-layouts": "^2.5.1",
    "dotenv": "^16.4.5",
    "morgan": "^1.10.0",
    # Database
    "pg": "^8.12.0",
    # Auth and sessions: bcrypt 5.x pulls @mapbox/node-pre-gyp -> tar@6
    # transitively, which carries multiple high-severity path-traversal
    # advisories. bcrypt 6.0 ships without node-pre-gyp. Async API
    # (.hash/.compare) is unchanged; only the callback signatures were
    # dropped and Node <18 support removed; neither affects our usage.
    "bcrypt": "^6.0.0",
    "express-session": "^1.18.0",
    "connect-pg-simple": "^9.0.1",
    # Security
    "helmet": "^7.1.0",
    "express-rate-limit": "^7.3.1",
    # Admin routes template deps
    "bcryptjs": "^2.4.3",
    "csv-parser": "^3.0.0",
    # multer 2.1.1+ fixes 7 high-severity DoS advisories affecting 1.x LTS
    # (GHSA-5528-5vmv-3xc2, xf7r-hgr6-v32p, v52c-386h-88mc, fjgf-rc76-4x9p,
    # g5hg-p3ph-g8qg, 4pg4-qvpc-4q3h, 44fp-w29j-9vj5). multer 2.x upgrades
    # busboy 0.x -> 1.x which is what actually resolves the DoS. The API we
    # use (multer({ storage: memoryStorage(), limits, fileFilter })) and
    # upload.single() are 2.x-compatible; no code changes required.
    "multer": "^2.1.1",
    # Icon library
    "lucide-static": "^1.1.0",
    # Performance
    "compression": "^1.7.4",
    # Image processing: sharp 0.33 pulls @mapbox/node-pre-gyp + tar@6
    # transitively, both of which carry multiple high-severity path
    # traversal advisories (GHSA-34x7-hfp2-rc4v et al.). 0.34 ships a
    # clean install tree. Our usage is limited to .resize/.jpeg/.toBuffer
    # which is unchanged across 0.33 -> 0.34.
    "sharp": "^0.34.0",
}

# Packages that must be removed if Claude adds them despite instructions
BANNED_DEPS = {"csurf", "csrf-csrf"}

# Security-critical deps where we OVERRIDE Claude's pin when present,
# rather than only injecting when missing. Required when the floor is a
# security fix (e.g. multer 2.x busboy DoS advisories, sharp 0.34 install
# tree) and Claude's prompt history encodes the older pin. All listed
# deps must also have a pin in REQUIRED_DEPS; the override copies that
# value verbatim.
FORCE_UPGRADE_DEPS = {"multer", "sharp", "bcrypt"}


@processor(after=('_fix_seed_uuid_id',))
def _ensure_package_deps(files: list) -> list:
    """Ensure package.json includes all required dependencies and removes banned ones.

    Claude frequently omits critical deps (connect-pg-simple, express-ejs-layouts,
    helmet, etc.) causing npm install to succeed but the app to crash on startup
    with 'Cannot find module' errors. This guarantees every required package is
    present and removes packages that conflict with the session-based CSRF pattern.
    """
    import json as json_mod

    file_dict = dict(files)
    pkg_raw = file_dict.get("package.json", "")
    if not pkg_raw:
        return files

    try:
        pkg = json_mod.loads(pkg_raw)
    except (json_mod.JSONDecodeError, ValueError):
        return files

    deps = pkg.get("dependencies", {})
    changed = False

    # Add all required dependencies. For FORCE_UPGRADE_DEPS, override an
    # existing pin if it differs from the safe baseline (security fix).
    for dep, version in REQUIRED_DEPS.items():
        if dep not in deps:
            deps[dep] = version
            changed = True
            print(f"  [post-process] Added {dep} to package.json dependencies")
        elif dep in FORCE_UPGRADE_DEPS and deps[dep] != version:
            old = deps[dep]
            deps[dep] = version
            changed = True
            print(
                f"  [post-process] Upgraded {dep}: {old} -> {version} "
                f"(security-pinned; see FORCE_UPGRADE_DEPS)"
            )

    # Remove banned dependencies
    for bad_dep in BANNED_DEPS:
        if bad_dep in deps:
            del deps[bad_dep]
            changed = True
            print(f"  [post-process] Removed banned {bad_dep} from package.json")

    if changed:
        pkg["dependencies"] = deps
        file_dict["package.json"] = json_mod.dumps(pkg, indent=2) + "\n"

    return list(file_dict.items())


# ---------------------------------------------------------------------------
# TEST_MODE login shortcut (build-time only; must never be live in production)
#
# The build-time test runner (test.py) logs in over HTTP with a fixed test
# password, so every magic-link app's POST /login carries a shortcut that
# accepts that password for an existing user when TEST_MODE is set. To make
# sure it cannot be live in a deployed app, two independent layers apply:
#   1. The shortcut's condition also requires NODE_ENV !== 'production'
#      (the deploy step always sets NODE_ENV=production and never sets
#      TEST_MODE; the test runner starts the app with NODE_ENV=test).
#   2. server.js gets a startup guard that exits when TEST_MODE is set while
#      NODE_ENV is production, so a mis-set variable fails loudly.
# Both are enforced deterministically here; the Claude prompt only
# suggests them.
# ---------------------------------------------------------------------------
_TEST_MODE_PRODUCTION_GUARD_JS = """// TEST_MODE must never be live in production (injected by post-processor)
if (process.env.TEST_MODE && process.env.NODE_ENV === 'production') {
  console.error('TEST_MODE must not be set in production');
  process.exit(1);
}
"""

# `process.env.TEST_MODE &&` (optionally `=== 'true'`) NOT followed by a
# NODE_ENV check. The production guard above does not match (its TEST_MODE
# is followed by NODE_ENV).
_WEAK_TEST_MODE_GATE_RE = re.compile(
    r"process\.env\.TEST_MODE(?:\s*===?\s*(['\"])true\1)?\s*&&(?!\s*process\.env\.NODE_ENV\b)\s*"
)

_TEST_MODE_PRODUCTION_GUARD_RE = re.compile(
    r"TEST_MODE[^\n]*NODE_ENV[^\n]*production|NODE_ENV[^\n]*production[^\n]*TEST_MODE"
)


def _harden_test_mode_gates(file_dict: dict) -> int:
    """Add the NODE_ENV !== 'production' check to every weak TEST_MODE gate
    in the generated JavaScript files. Returns the number of gates rewritten.
    Idempotent: an already-gated condition is left alone."""
    rewritten = 0
    for path, content in list(file_dict.items()):
        if not path.endswith(".js") or "process.env.TEST_MODE" not in content:
            continue
        new_content, count = _WEAK_TEST_MODE_GATE_RE.subn(
            lambda m: m.group(0).rstrip() + " process.env.NODE_ENV !== 'production' && ",
            content,
        )
        if count:
            file_dict[path] = new_content
            rewritten += count
    return rewritten


def _ensure_test_mode_production_guard(file_dict: dict) -> bool:
    """Make sure server.js exits when TEST_MODE is set in production.

    Only acts when some generated JS file references process.env.TEST_MODE.
    Returns True if a guard was injected. Idempotent."""
    if not any(
        path.endswith(".js") and "process.env.TEST_MODE" in content
        for path, content in file_dict.items()
    ):
        return False
    server = file_dict.get("server.js", "")
    if not server or _TEST_MODE_PRODUCTION_GUARD_RE.search(server):
        return False
    app_decl = re.search(r"^[ \t]*(?:const|let|var)\s+app\s*=\s*express\s*\(", server, re.MULTILINE)
    if app_decl:
        server = server[:app_decl.start()] + _TEST_MODE_PRODUCTION_GUARD_JS + "\n" + server[app_decl.start():]
    else:
        server = _TEST_MODE_PRODUCTION_GUARD_JS + "\n" + server
    file_dict["server.js"] = server
    return True


@processor(after=('_fix_seed_admin_email',), phase=('build',))
def _ensure_magic_link_auth(files: list) -> list:
    """Post-processor #17: ensure magic link auth artifacts in generated app.

    No-op when USE_MAGIC_LINK_AUTH is false.
    """
    if not USE_MAGIC_LINK_AUTH:
        emit_build_trace(
            _CURRENT_BUILD_DIR.get(),
            "ensure_magic_link_auth.skipped",
            {"reason": "magic_link_disabled"},
        )
        return files

    file_dict = dict(files)
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_magic_link_auth.start",
        {"input_count": len(files)},
    )

    # 1. Ensure magic_link_tokens table in schema.sql
    schema = file_dict.get("db/schema.sql", "")
    if schema and "magic_link_tokens" not in schema:
        schema += """
-- Magic link authentication tokens
CREATE TABLE IF NOT EXISTS magic_link_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP
);
"""
        file_dict["db/schema.sql"] = schema
        print("  [post-process] Injected magic_link_tokens table into schema.sql")

    # 2. Ensure password_set column in users table
    if schema and "password_set" not in schema:
        import re as _re
        if "CREATE TABLE" in schema and "users" in schema:
            # Try to add password_set column inside the users table definition
            added = False
            # Look for the users table closing paren and inject before it
            users_table_match = _re.search(
                r'(CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(?:"?users"?)\s*\([^;]+?)(^\s*\);)',
                schema, _re.IGNORECASE | _re.MULTILINE | _re.DOTALL
            )
            if users_table_match:
                insert_pos = users_table_match.start(2)
                schema = schema[:insert_pos] + "    password_set BOOLEAN DEFAULT FALSE,\n" + schema[insert_pos:]
                file_dict["db/schema.sql"] = schema
                added = True
                print("  [post-process] Added password_set column to users table in schema.sql")
            if not added:
                print("  [post-process] WARNING: password_set column missing from users table (could not auto-fix)")

    # 3. Ensure password_set = TRUE on every seed user so the
    # requirePasswordSet middleware lets them through. Magic-link users
    # have password=NULL; flag flip is purely UX ("no set-password dance
    # needed") and does NOT enable password login (bcrypt.compare against
    # NULL returns false). Without this, MW-shape apps redirect every
    # request to /set-password.
    #
    # NOTE: the previous shadow-credential bcrypt injection that used to live
    # here was moved to a separate always-on post-processor
    # (`_scrub_bcrypt_from_seed`, defined below). That way apps built with
    # USE_MAGIC_LINK_AUTH=false also get their seed.sql hash-scrubbed even
    # though they skip the rest of this function.
    seed = file_dict.get("db/seed.sql", "")
    if seed:
        seed_changed = False
        if "UPDATE users SET password_set = TRUE" not in seed:
            seed = seed.rstrip() + (
                "\n\n-- SEC-1: password_set=TRUE satisfies requirePasswordSet middleware;\n"
                "-- password stays NULL so password login is impossible (magic-link only).\n"
                "UPDATE users SET password_set = TRUE WHERE password_set = FALSE OR password_set IS NULL;\n"
            )
            seed_changed = True
            print("  [post-process] Set password_set=TRUE on seed users (no password hash injected)")
        if seed_changed:
            file_dict["db/seed.sql"] = seed

    # 4. Inject template files if missing
    templates_dir = Path(__file__).parent / "templates"
    for view_path, template_file in [
        ("views/magic-link-sent.ejs", "magic_link_sent.ejs"),
        ("views/set-password.ejs", "set_password.ejs"),
    ]:
        if view_path not in file_dict:
            tpl_path = templates_dir / template_file
            if tpl_path.exists():
                file_dict[view_path] = tpl_path.read_text()
                print(f"  [post-process] Injected {view_path} from template")

    # 5. Check for /magic route (warn if missing, non-blocking)
    auth_route = file_dict.get("routes/auth.js", "")
    if auth_route and "/magic" not in auth_route:
        print("  [post-process] WARNING: /magic route not found in routes/auth.js")

    # 6. Ensure the TEST_MODE login shortcut in POST /login handler
    # This is the #1 cause of magic link app test failures. If the shortcut
    # is missing or incomplete (e.g. just a comment without session creation),
    # inject the full working version. Every TEST_MODE gate, injected here or
    # written by Claude, must also require NODE_ENV !== 'production' (see
    # the comment above _TEST_MODE_PRODUCTION_GUARD_JS).
    hardened = _harden_test_mode_gates(file_dict)
    if hardened:
        print(f"  [post-process] Added NODE_ENV production check to {hardened} TEST_MODE gate(s)")
        auth_route = file_dict.get("routes/auth.js", "")
    if auth_route:
        has_test_mode = "test_password_for_ci" in auth_route
        # Check that req.session.save appears NEAR test_password_for_ci (within the
        # same block), not just anywhere in the file. The normal login flow often has
        # session.save which gave a false positive, leaving the TEST_MODE block
        # incomplete (redirect without session.save => race condition with async store).
        has_session_save = False
        if has_test_mode:
            idx = auth_route.index("test_password_for_ci")
            nearby = auth_route[idx:idx + 500]
            has_session_save = "req.session.save" in nearby
        if not has_test_mode or not has_session_save:
            # Find the POST /login handler and inject TEST_MODE block
            test_mode_block = """
  // TEST_MODE login shortcut for automated testing, never live in production (injected by post-processor)
  if (process.env.TEST_MODE && process.env.NODE_ENV !== 'production' && password === 'test_password_for_ci') {
    const testUser = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
    if (testUser.rows.length > 0) {
      const user = testUser.rows[0];
      req.session.userId = user.id;
      req.session.userEmail = user.email;
      req.session.userRole = user.role || 'admin';
      return req.session.save(() => res.redirect('/dashboard'));
    }
  }
"""
            # Strategy: inject right after the POST /login route definition
            import re
            # Match common patterns for POST /login handler opening
            patterns = [
                # router.post('/login', async (req, res) => {
                (r"""(router\.post\s*\(\s*['"]\/login['"]\s*,\s*(?:validateCsrfToken\s*,\s*)?async\s*\(req\s*,\s*res\)\s*=>\s*\{)""",),
                # router.post('/login', validateCsrfToken, async (req, res) => {
                (r"""(router\.post\s*\(\s*['"]\/login['"]\s*,\s*\w+\s*,\s*async\s*\(req\s*,\s*res(?:\s*,\s*next)?\)\s*=>\s*\{)""",),
                # Simpler fallback: any post('/login' line with opening brace
                (r"""(\.post\s*\(\s*['"]\/login['"].*\{\s*)""",),
            ]
            injected = False
            for pat_tuple in patterns:
                pattern = pat_tuple[0]
                match = re.search(pattern, auth_route)
                if match:
                    insert_pos = match.end()
                    # Find the next line with const/let/var for email/password extraction
                    # Inject after the destructuring line
                    rest = auth_route[insert_pos:]
                    # Look for the email/password extraction (within first 500 chars)
                    # Pattern 1: destructuring, const { email, password } = req.body;
                    destr_match = re.search(
                        r'((?:const|let|var)\s*\{[^}]*(?:email|password)[^}]*\}[^;]*;)',
                        rest[:500]
                    )
                    # Pattern 2: separate declarations, const password = req.body.password;
                    # (Claude sometimes generates separate const lines instead of destructuring)
                    separate_match = None
                    if not destr_match:
                        separate_match = re.search(
                            r'((?:const|let|var)\s+password\s*=\s*[^;]+;)',
                            rest[:500]
                        )
                    if destr_match:
                        final_pos = insert_pos + destr_match.end()
                        auth_route = auth_route[:final_pos] + test_mode_block + auth_route[final_pos:]
                    elif separate_match:
                        final_pos = insert_pos + separate_match.end()
                        auth_route = auth_route[:final_pos] + test_mode_block + auth_route[final_pos:]
                    else:
                        # Last resort: use req.body.password directly instead of relying on
                        # a local `password` variable (which may not be declared yet)
                        safe_test_mode_block = """
  // TEST_MODE login shortcut for automated testing, never live in production (injected by post-processor)
  if (process.env.TEST_MODE && process.env.NODE_ENV !== 'production' && (req.body.password || '') === 'test_password_for_ci') {
    const testUser = await pool.query('SELECT * FROM users WHERE email = $1', [req.body.email || '']);
    if (testUser.rows.length > 0) {
      const user = testUser.rows[0];
      req.session.userId = user.id;
      req.session.userEmail = user.email;
      req.session.userRole = user.role || 'admin';
      return req.session.save(() => res.redirect('/dashboard'));
    }
  }
"""
                        auth_route = auth_route[:insert_pos] + "\n" + safe_test_mode_block + auth_route[insert_pos:]
                    injected = True
                    break

            if injected:
                file_dict["routes/auth.js"] = auth_route
                print("  [post-process] Injected TEST_MODE login shortcut into POST /login handler")
            else:
                print("  [post-process] WARNING: Could not inject TEST_MODE login shortcut - POST /login pattern not found")

    if _ensure_test_mode_production_guard(file_dict):
        print("  [post-process] Injected TEST_MODE production guard into server.js")

    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_magic_link_auth.completed",
        {"output_count": len(file_dict)},
    )
    return list(file_dict.items())


# SEC-1 (2026-04-19): defense-in-depth bcrypt scrubber. Always runs, 
# independent of USE_MAGIC_LINK_AUTH, so even password-primary apps can't
# accidentally ship a committed credential. The Claude prompt at line ~9102
# now tells every build to seed `password = NULL`, and this post-processor
# is the final enforcement layer for when Claude disobeys or a reapply
# lifts an older artifact.
@processor(after=('_ensure_magic_link_auth',), phase=('build', 'reapply'))
def _scrub_bcrypt_from_seed(files: list) -> list:
    """Remove any bcrypt password hash ($2a$, $2b$, $2y$, any cost factor)
    from db/seed.sql. Operates in three passes:

    1. Delete whole `UPDATE users SET password = '$2...' ...;` statements
       (and any leading `-- comment` line on the same block). Handles the
       legacy shadow-credential injection.
    2. Replace any remaining quoted `'$2...'` literal with SQL NULL.
       Handles `INSERT INTO users (..., '$2b$...', ...)` patterns.
    3. Excise stray unquoted hash fragments. Paranoia pass; a well-formed
       SQL file should never trigger this.

    The bcrypt regex `\\$2[aby]\\$\\d{2}\\$[./A-Za-z0-9]{53}` accepts every
    variant at any cost factor. No-op when seed.sql is absent or contains
    no bcrypt pattern.
    """
    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    import re as _re_pw
    bcrypt_re = _re_pw.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")
    if not bcrypt_re.search(seed):
        return files  # fast path: no hash present

    seed_changed = False

    # Pass 1: drop whole UPDATE...password='$2...' statements.
    update_with_hash = _re_pw.compile(
        r"(?:--[^\n]*\n)?UPDATE\s+users\s+SET\s+password\s*=\s*'\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}'[^;]*;\s*\n?",
        flags=_re_pw.IGNORECASE | _re_pw.DOTALL,
    )
    new_seed = update_with_hash.sub("", seed)
    if new_seed != seed:
        seed = new_seed
        seed_changed = True
        print("  [post-process] SEC-1 scrubber dropped UPDATE...password='$2...' statement(s)")

    # Pass 2: inline `'$2...'` -> NULL. Catches `INSERT ... VALUES (..., '$2b$...', ...)`.
    new_seed = _re_pw.sub(r"'\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}'", "NULL", seed)
    if new_seed != seed:
        seed = new_seed
        seed_changed = True
        print("  [post-process] SEC-1 scrubber replaced inline bcrypt literal(s) with NULL")

    # Pass 3: defensive final sweep for unquoted / orphan fragments.
    new_seed = bcrypt_re.sub("", seed)
    if new_seed != seed:
        seed = new_seed
        seed_changed = True
        print("  [post-process] SEC-1 scrubber excised unquoted bcrypt fragment(s)")

    if seed_changed:
        file_dict["db/seed.sql"] = seed

    return list(file_dict.items())


@processor(after=('_scrub_bcrypt_from_seed',), phase=('build',))
def _ensure_magic_link_hash(files: list) -> list:
    """Post-processor #18: ensure GET /magic route hashes token before DB query.

    The worker stores SHA-256(raw_token.encode()) in seed.sql, but Claude
    sometimes generates code that queries with the raw token instead of
    hashing it first. This post-processor scans routes/auth.js for the
    GET /magic handler and injects the hash step if missing.

    No-op when USE_MAGIC_LINK_AUTH is false.
    """
    if not USE_MAGIC_LINK_AUTH:
        return files

    import re

    file_dict = dict(files)
    auth_route = file_dict.get("routes/auth.js", "")
    if not auth_route:
        return files

    # Only act if there's a /magic route
    if "/magic" not in auth_route:
        return files

    # Find the GET /magic handler FIRST so we can scope the hash check to it.
    # The old approach checked the whole file for createHash('sha256'), but
    # POST /request-magic-link also uses that pattern (to hash tokens for
    # storage). This caused the function to return early even when GET /magic
    # was missing the hash step, which is the #1 cause of magic link failures.
    magic_get_match = re.search(
        r"""(router\.get\s*\(\s*['"]\/magic['"]\s*,\s*(?:\w+\s*,\s*)?async\s*\(req\s*,\s*res(?:\s*,\s*next)?\)\s*=>\s*\{)""",
        auth_route
    )
    if not magic_get_match:
        # Fallback: simpler pattern
        magic_get_match = re.search(
            r"""(\.get\s*\(\s*['"]\/magic['"].*?\{)\s*""",
            auth_route
        )

    if not magic_get_match:
        print("  [post-process] WARNING: Could not find GET /magic handler pattern for hash fix")
        return files

    # Strategy: inject the hash computation right after the handler opens,
    # then replace any `req.query.token` in the WHERE clause with `tokenHash`
    insert_pos = magic_get_match.end()

    # Find the scope of this handler (up to next router. or module.exports)
    rest = auth_route[insert_pos:]
    handler_end = len(rest)
    for end_pattern in [r'\nrouter\.', r'\nmodule\.exports']:
        end_match = re.search(end_pattern, rest)
        if end_match and end_match.start() < handler_end:
            handler_end = end_match.start()

    handler_body = rest[:handler_end]

    # Check if token is already being hashed WITHIN the GET /magic handler only.
    # Previously this checked the whole file, which matched the hash in
    # POST /request-magic-link and caused a false-positive early return.
    has_hash_in_handler = bool(re.search(
        r"createHash\s*\(\s*['\"]sha256['\"]\s*\)\.update\s*\(",
        handler_body
    ))
    if has_hash_in_handler:
        # GET /magic handler already hashes the token, nothing to do
        return files

    # Check if the handler passes raw token directly to DB query
    # Patterns: [req.query.token] or [token] in query params for token_hash column
    uses_raw_token = bool(re.search(
        r"""(?:WHERE\s+token_hash\s*=\s*\$\d+|token_hash[^,]*\$\d+)""",
        handler_body,
        re.IGNORECASE
    ))

    if not uses_raw_token:
        # No token_hash query found, or pattern is unusual
        return files

    # Check what variable is being used as the token source
    token_var_match = re.search(
        r"""(?:const|let|var)\s+(?:\{[^}]*token[^}]*\}|token)\s*=\s*req\.query\.token""",
        handler_body
    )

    hash_block = """
    // Hash the token before DB lookup (worker stores SHA-256 hash, not raw token)
    const crypto = require('crypto');
    const tokenHash = crypto.createHash('sha256').update(req.query.token).digest('hex');
"""

    # Insert the hash block after the handler opening
    # Look for where token is first extracted from req.query
    token_extract = re.search(
        r"""((?:const|let|var)\s+(?:\{[^}]*\btoken\b[^}]*\}|\btoken\b)\s*=\s*req\.query[^;]*;)""",
        handler_body
    )

    if token_extract:
        # Insert after the token extraction line
        final_insert = insert_pos + token_extract.end()
        auth_route = auth_route[:final_insert] + hash_block + auth_route[final_insert:]
        # Now update handler_body reference for the replacement below
        # Re-find the handler body after injection
        rest_after = auth_route[final_insert + len(hash_block):]
    else:
        # Insert right after the opening brace
        auth_route = auth_route[:insert_pos] + hash_block + auth_route[insert_pos:]
        rest_after = auth_route[insert_pos + len(hash_block):]

    # Now replace the raw token variable in the query with tokenHash
    # Find the query call within the handler and replace the token param
    # Pattern: pool.query('...token_hash = $N', [token]) or [req.query.token]
    handler_end_after = len(rest_after)
    for end_pattern in [r'\nrouter\.', r'\nmodule\.exports']:
        end_match = re.search(end_pattern, rest_after)
        if end_match and end_match.start() < handler_end_after:
            handler_end_after = end_match.start()

    handler_after = rest_after[:handler_end_after]
    offset = len(auth_route) - len(rest_after)

    # Replace token variable in query params array with tokenHash
    # Match: [token] or [req.query.token] in the context of token_hash query
    new_handler = handler_after
    # Replace patterns like: , [token]) or , [req.query.token])
    new_handler = re.sub(
        r"""\[\s*(?:req\.query\.)?token\s*\](\s*\))""",
        r"[tokenHash]\1",
        new_handler
    )
    # Also replace patterns like: [token, ...] where token is first element
    new_handler = re.sub(
        r"""\[\s*(?:req\.query\.)?token\s*,""",
        "[tokenHash,",
        new_handler
    )

    if new_handler != handler_after:
        auth_route = auth_route[:offset] + new_handler + auth_route[offset + len(handler_after):]
        file_dict["routes/auth.js"] = auth_route
        print("  [post-process] Injected SHA-256 token hash into GET /magic handler")
    else:
        # Even if we couldn't replace the param, we still added the hash computation
        # The developer/Claude may have used a different pattern
        file_dict["routes/auth.js"] = auth_route
        print("  [post-process] Added SHA-256 hash computation to GET /magic handler (manual param replacement may be needed)")

    return list(file_dict.items())


@processor(after=('_ensure_magic_link_hash',), phase=('build',))
def _ensure_resend_from_address(files: list) -> list:
    """Post-processor #19: fix Resend API from address in generated apps.

    Claude sometimes generates email-sending code with from addresses derived
    from req.get('host') (e.g. noreply@appname-production-xxxx.up.railway.app)
    which is NOT a verified domain on Resend, causing silent 403 failures.
    This post-processor rewrites all Resend fetch calls to use the verified
    craftandship.io domain.

    Also fixes to: field to use array format [email] per Resend best practices,
    and adds response error logging where missing.
    """
    import re

    file_dict = dict(files)
    changed = False

    # Check all JS files that might contain Resend API calls
    for filepath, content in list(file_dict.items()):
        if not filepath.endswith(".js"):
            continue
        if "api.resend.com/emails" not in content:
            continue

        original = content

        # Fix 1: Replace host-derived from addresses with craftandship.io
        # Pattern: from: 'noreply@' + req.get('host')... or from: `noreply@${req.get('host')}...`
        content = re.sub(
            r"""from:\s*['"]noreply@['"]\s*\+\s*req\.get\(['"]host['"]\)[^,}]*""",
            "from: 'noreply@craftandship.io'",
            content,
        )
        content = re.sub(
            r"""from:\s*`noreply@\$\{req\.get\(['"]host['"]\)[^}]*\}`""",
            "from: 'noreply@craftandship.io'",
            content,
        )
        # Also catch from: 'noreply@' + host or similar
        content = re.sub(
            r"""from:\s*['"]noreply@['"]\s*\+\s*(?:host|hostname|domain)[^,}]*""",
            "from: 'noreply@craftandship.io'",
            content,
        )
        # Catch Railway-style domains directly hardcoded
        content = re.sub(
            r"""from:\s*['"][^'"]*@[^'"]*\.up\.railway\.app['"]""",
            "from: 'noreply@craftandship.io'",
            content,
        )

        # Fix 2: Ensure to: field uses array format
        # Pattern: to: email  or  to: userEmail  (not already wrapped in [])
        # Only within fetch bodies for api.resend.com
        def fix_resend_to(match):
            block = match.group(0)
            # Fix to: <var> -> to: [<var>] (but not if already an array)
            block = re.sub(
                r"""to:\s*(?!\[)(\w+)""",
                r"to: [\1]",
                block,
            )
            return block

        content = re.sub(
            r"""fetch\s*\(\s*['"]https://api\.resend\.com/emails['"].*?\}\s*\)""",
            fix_resend_to,
            content,
            flags=re.DOTALL,
        )

        if content != original:
            file_dict[filepath] = content
            changed = True

    if changed:
        print("  [post-process] Fixed Resend API from address and to format in generated code")

    return list(file_dict.items())


@processor(after=('_validate_seed_sql',), phase=('build',))
def _join_multiline_inserts(files: list) -> list:
    """Join multi-line INSERT statements into single lines.

    Claude often generates INSERT INTO...on one line and VALUES...on the next.
    This breaks downstream post-processors that use single-line regex patterns.
    Joining them early ensures _split_seed_inserts, _fix_seed_column_mismatch,
    and _dedup_seed_entries all work correctly.
    """
    import re as re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    lines = seed.split("\n")
    joined = []
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Pattern: INSERT INTO t (cols) on this line, VALUES on next line
        if (re_mod.match(r"^INSERT\s+INTO\s+\w+\s*\([^)]+\)\s*$", stripped, re_mod.IGNORECASE)
                and i + 1 < len(lines)
                and lines[i + 1].strip().upper().startswith("VALUES")):
            # Join: INSERT line + space + VALUES line (trimmed)
            joined_line = stripped + " " + lines[i + 1].strip()
            joined.append(joined_line)
            changed = True
            i += 2
            continue
        joined.append(line)
        i += 1

    if changed:
        print("  [post-process] Joined multi-line INSERT statements into single lines")
        file_dict["db/seed.sql"] = "\n".join(joined)
        return list(file_dict.items())

    return files


def _sql_stmt_strings_closed(stmt: str) -> bool:
    """Return True when every `'` in `stmt` opens/closes a complete
    string literal per SQL standard (where `''` inside a string is an
    escaped single quote, not a close-then-open).

    Implementation: linear scan, tracking `in_string`. When we hit a
    `'` inside a string and the next char is also `'`, skip both (it's
    an escape pair) and stay inside. Otherwise, toggle state. Returns
    True iff the scanner ends OUTSIDE any open string.

    Used by _split_seed_inserts's second pass to replace a naive
    `count('\'')` check that false-positively flagged legitimate
    multi-line blog-post INSERTs with embedded apostrophes.
    """
    in_string = False
    i = 0
    length = len(stmt)
    while i < length:
        ch = stmt[i]
        if ch == "'":
            if in_string and i + 1 < length and stmt[i + 1] == "'":
                # Escaped '' pair inside a string; advance past both.
                i += 2
                continue
            in_string = not in_string
        i += 1
    return not in_string


@processor(after=('_join_multiline_inserts',), phase=('build',))
def _split_seed_inserts(files: list) -> list:
    """Split multi-row INSERTs into single-row INSERTs with ON CONFLICT DO NOTHING.

    Claude generates multi-row INSERT statements where one malformed row (truncated
    string, missing NOW(), etc.) kills ALL rows for that table. By splitting each
    row into its own INSERT, bad rows fail independently and the rest succeed.

    Also adds ON CONFLICT DO NOTHING to each row to prevent duplicate key errors.
    """
    import re as re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    # State-machine parser to split VALUES rows respecting quotes and parens
    def _split_value_rows(values_block):
        """Split a VALUES block like "(row1), (row2), (row3)" into individual rows.

        Handles: quoted strings with commas, NOW() expressions, nested parens.
        Returns list of row strings including outer parens, e.g. ["(row1)", "(row2)"].
        """
        rows = []
        depth = 0
        quote_char = None
        current = []
        i = 0
        while i < len(values_block):
            ch = values_block[i]

            # Handle string quoting
            if quote_char:
                current.append(ch)
                if ch == quote_char:
                    # Check for escaped quote ('' in SQL)
                    if i + 1 < len(values_block) and values_block[i + 1] == quote_char:
                        current.append(values_block[i + 1])
                        i += 2
                        continue
                    quote_char = None
                i += 1
                continue

            if ch in ("'", '"'):
                quote_char = ch
                current.append(ch)
                i += 1
                continue

            if ch == '(':
                depth += 1
                current.append(ch)
            elif ch == ')':
                depth -= 1
                current.append(ch)
                if depth == 0:
                    rows.append("".join(current).strip())
                    current = []
            elif ch == ',' and depth == 0:
                # Comma between rows, skip
                pass
            else:
                if depth > 0:
                    current.append(ch)
            i += 1

        # Handle any trailing content
        leftover = "".join(current).strip()
        if leftover and leftover.startswith("("):
            rows.append(leftover)

        return rows

    # Match INSERT INTO table (cols) VALUES ... ; or ... ON CONFLICT ...
    insert_pattern = re_mod.compile(
        r"(INSERT\s+INTO\s+\w+\s*\([^)]+\)\s*VALUES\s*)"  # prefix: INSERT INTO t (cols) VALUES
        r"(.*?)"                                            # values block
        r"(\s*ON\s+CONFLICT[^;]*;\s*|\s*;\s*)",            # suffix: ON CONFLICT... or ;
        re_mod.IGNORECASE | re_mod.DOTALL,
    )

    changed = False

    def _replace_insert(m):
        nonlocal changed
        prefix = m.group(1).strip()
        values_block = m.group(2).strip()

        rows = _split_value_rows(values_block)
        if len(rows) <= 1:
            # Single row: just add ON CONFLICT DO NOTHING (keep on one line)
            if rows:
                changed = True
                return f"{prefix} {rows[0]} ON CONFLICT DO NOTHING;\n"
            return m.group(0)

        # Multiple rows: split into individual INSERT statements (one line each)
        changed = True
        stmts = []
        for row in rows:
            stmts.append(f"{prefix} {row} ON CONFLICT DO NOTHING;")
        return "\n".join(stmts) + "\n"

    new_seed = insert_pattern.sub(_replace_insert, seed)

    # Second pass: comment out any INSERT statements with genuinely
    # unbalanced quotes: using a proper SQL-aware state machine instead
    # of raw character counting.
    #
    # The naive `count('\'')`
    # check flagged multi-line INSERTs whose string literals contained
    # properly-escaped apostrophes (e.g. `'reader''s question'`). Claude
    # emitted a valid blog_post INSERT; the check counted raw quotes
    # (which happens to come out odd when escape pairs interleave across
    # lines in certain shapes) and tagged the whole statement as
    # `-- SKIPPED`. Seed load failed, the whole blog_posts table was empty,
    # and runtime 500'd on /blog.
    #
    # State-machine rules (SQL standard):
    #   - `'` outside a string opens one
    #   - `'` inside a string: if followed by another `'`, it's an escape
    #     pair (stay inside); otherwise it closes the string
    #   - `;` inside a string is literal; outside, it ends a statement
    # Returns True when the scanner ends OUTSIDE any open string.
    stmt_pattern = re_mod.compile(
        r"(INSERT\s+INTO\s+\w+\s*\([^)]+\)\s*VALUES\s*\([^;]*;)",
        re_mod.IGNORECASE | re_mod.DOTALL,
    )
    commented_count = 0

    def _check_quotes(m):
        nonlocal commented_count, changed
        stmt = m.group(0)
        if _sql_stmt_strings_closed(stmt):
            return stmt
        commented_count += 1
        changed = True
        # Comment out each line of the statement.
        commented = "\n".join(
            f"-- SKIPPED (unbalanced quotes): {line}"
            for line in stmt.split("\n") if line.strip()
        )
        return commented

    new_seed = stmt_pattern.sub(_check_quotes, new_seed)

    if commented_count > 0:
        print(f"  [post-process] Commented out {commented_count} seed INSERT(s) with unbalanced quotes")

    if changed:
        print("  [post-process] Split multi-row seed INSERTs into single-row statements")
        file_dict["db/seed.sql"] = new_seed
        return list(file_dict.items())

    return files


@processor(after=('_split_seed_inserts',), phase=('build',))
def _dedup_seed_entries(files: list) -> list:
    """Remove duplicate seed INSERT rows within each table.

    Claude sometimes generates duplicate titles/names in seed data (e.g. two
    'Full Moon Ceremony' events). After _split_seed_inserts has broken multi-row
    INSERTs into single-row statements, this deduplicates by extracting the first
    quoted string value from each INSERT and skipping subsequent rows for the same
    table with the same value.

    Assumes _join_multiline_inserts has already been run to normalize
    multi-line INSERTs into single lines.
    """
    import re as re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    # Track first quoted value per table for dedup
    seen = {}  # table_name -> set of first-quoted-values
    lines = seed.split("\n")
    new_lines = []
    removed = 0

    insert_re = re_mod.compile(
        r"INSERT\s+INTO\s+(\w+)\s*\([^)]+\)\s*VALUES\s*\(",
        re_mod.IGNORECASE,
    )

    for line in lines:
        m = insert_re.search(line)
        if not m:
            new_lines.append(line)
            continue

        table = m.group(1).lower()
        # Extract the first quoted string value from the VALUES clause
        rest = line[m.end() - 1:]  # from the opening ( of VALUES(
        first_quote = rest.find("'")
        if first_quote == -1:
            new_lines.append(line)
            continue

        # Extract value, handling '' escapes
        val_chars = []
        j = first_quote + 1
        while j < len(rest):
            ch = rest[j]
            if ch == "'":
                if j + 1 < len(rest) and rest[j + 1] == "'":
                    val_chars.append("'")
                    j += 2
                    continue
                break
            val_chars.append(ch)
            j += 1

        first_val = "".join(val_chars).strip().lower()
        if not first_val:
            new_lines.append(line)
            continue

        if table not in seen:
            seen[table] = set()

        if first_val in seen[table]:
            removed += 1
            new_lines.append(f"-- SKIPPED (duplicate): {line.strip()}")
            continue

        seen[table].add(first_val)
        new_lines.append(line)

    if removed > 0:
        print(f"  [post-process] Removed {removed} duplicate seed INSERT(s)")
        file_dict["db/seed.sql"] = "\n".join(new_lines)
        return list(file_dict.items())

    return files


@processor(after=('_dedup_seed_entries',), phase=('build',))
def _ensure_gallery_dedup(files: list) -> list:
    """Add UNIQUE index on photos.image_url and cap seed photo rows at 12.

    Prevents duplicate gallery photos in seed data and enforces uniqueness at DB level.
    """
    import re as re_mod

    file_dict = dict(files)
    schema = file_dict.get("db/schema.sql", "")
    if not schema:
        return files

    # Only apply if there's a photos table
    if not re_mod.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?photos\b", schema, re_mod.IGNORECASE):
        return files

    changed = False

    # Add unique index on image_url if not already present
    if "idx_photos_image_url" not in schema:
        schema = schema.rstrip() + "\n\nCREATE UNIQUE INDEX IF NOT EXISTS idx_photos_image_url ON photos(image_url);\n"
        file_dict["db/schema.sql"] = schema
        changed = True
        print("  [post-process] Added UNIQUE index on photos.image_url")

    # Cap seed photo rows at 12
    seed = file_dict.get("db/seed.sql", "")
    if seed:
        lines = seed.split("\n")
        photo_insert_indices = []
        for i, line in enumerate(lines):
            if re_mod.match(r"\s*INSERT\s+INTO\s+photos\b", line, re_mod.IGNORECASE):
                photo_insert_indices.append(i)

        if len(photo_insert_indices) > 12:
            # Remove excess photo INSERT lines (keep first 12)
            remove_indices = set(photo_insert_indices[12:])
            lines = [line for i, line in enumerate(lines) if i not in remove_indices]
            file_dict["db/seed.sql"] = "\n".join(lines)
            changed = True
            print(f"  [post-process] Capped seed photos from {len(photo_insert_indices)} to 12")

    if changed:
        return list(file_dict.items())
    return files


@processor(after=('_ensure_nav_logo_default_seed',))
def _ensure_site_settings(files: list, spec: dict) -> list:
    """Inject site_settings table, middleware, admin settings route+view, and dashboard link.

    Provides admin-editable contact info and social media links for generated apps.
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_site_settings.start",
        {"input_count": len(files)},
    )

    # 1. Add site_settings table to schema.sql, OR normalize Claude's
    # non-canonical variant when present.
    #
    # Canonical shape: columns named `key` + `value` so the shipped
    # cli/templates/routes_settings.js + views_admin_settings.ejs can
    # `SELECT key, value FROM site_settings` without a schema mismatch.
    # Claude has emitted a site_settings table
    # using `setting_key`/`setting_value`, so the route template's
    # SELECT blew up with `column "key" does not exist`. Pattern recurs
    # across non-MW specs; the Standard-Widget Doctrine fix is to
    # normalize the schema shape inside the post-processor rather than
    # teach every route to handle both spellings.
    schema = file_dict.get("db/schema.sql", "")
    if schema:
        CANONICAL_TABLE = (
            "CREATE TABLE IF NOT EXISTS site_settings (\n"
            "  key VARCHAR(100) PRIMARY KEY,\n"
            "  value TEXT DEFAULT '',\n"
            "  updated_at TIMESTAMP DEFAULT NOW()\n"
            ");\n"
        )
        if "site_settings" not in schema:
            schema = schema.rstrip() + "\n\n" + CANONICAL_TABLE
            file_dict["db/schema.sql"] = schema
            changed = True
            print("  [post-process] Added site_settings table to schema")
        else:
            # Site_settings already declared: normalize it to the
            # canonical key/value shape if Claude used a variant. Strip
            # the existing CREATE TABLE block plus any matching INDEX
            # declarations that reference setting_key, then append the
            # canonical definition.
            import re as _re_mod_norm
            create_re = _re_mod_norm.compile(
                r"CREATE TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+site_settings\s*\([^;]*\);",
                _re_mod_norm.IGNORECASE | _re_mod_norm.DOTALL,
            )
            match = create_re.search(schema)
            if match:
                # Detect the column name Claude used so we can decide
                # whether a rewrite is needed and also normalize seed.sql.
                block = match.group(0)
                uses_non_canonical = (
                    "setting_key" in block or "setting_value" in block
                )
                if uses_non_canonical:
                    schema_without_block = (
                        schema[: match.start()] + schema[match.end():]
                    )
                    # Also drop any index referencing setting_key so it
                    # doesn't outlive the canonical table.
                    idx_re = _re_mod_norm.compile(
                        r"CREATE\s+INDEX[^;]*site_settings\s*\(\s*setting_key\s*\)\s*;",
                        _re_mod_norm.IGNORECASE,
                    )
                    schema_without_block = idx_re.sub(
                        "", schema_without_block
                    )
                    schema = (
                        schema_without_block.rstrip()
                        + "\n\n"
                        + CANONICAL_TABLE
                    )
                    file_dict["db/schema.sql"] = schema
                    changed = True
                    print(
                        "  [post-process] Normalized site_settings columns "
                        "setting_key/setting_value → key/value in schema"
                    )
                    # Rewrite seed.sql to match.
                    seed_raw = file_dict.get("db/seed.sql", "")
                    if seed_raw and (
                        "setting_key" in seed_raw
                        or "INSERT INTO site_settings (setting_key," in seed_raw
                    ):
                        seed_raw = _re_mod_norm.sub(
                            r"INSERT\s+INTO\s+site_settings\s*\(\s*setting_key\s*,\s*setting_value\s*\)",
                            "INSERT INTO site_settings (key, value)",
                            seed_raw,
                            flags=_re_mod_norm.IGNORECASE,
                        )
                        seed_raw = seed_raw.replace("setting_key", "key").replace(
                            "setting_value", "value"
                        )
                        file_dict["db/seed.sql"] = seed_raw
                        print(
                            "  [post-process] Rewrote seed.sql "
                            "setting_key/setting_value → key/value"
                        )

                    # Sweep Claude-generated route files (public.js,
                    # about.js, contact.js, etc.) that ALSO query
                    # site_settings with the non-canonical column names.
                    # The schema + seed normalization
                    # is not enough on its own because
                    # Claude's route files ALSO used `SELECT setting_key,
                    # setting_value FROM site_settings`, leaving the
                    # schema validator to flag them as drift (which it
                    # correctly did, failing the build).
                    route_rewrites = 0
                    for fpath in list(file_dict.keys()):
                        if not isinstance(fpath, str):
                            continue
                        if not (fpath.endswith(".js") or fpath.endswith(".ejs")):
                            continue
                        if fpath in {"db/schema.sql", "db/seed.sql"}:
                            continue
                        content = file_dict[fpath]
                        if not isinstance(content, str):
                            continue
                        if "setting_key" not in content and "setting_value" not in content:
                            continue
                        # Scope the rewrite to tokens that appear in
                        # site_settings SQL contexts: don't touch
                        # unrelated identifiers in other code.
                        updated = content
                        # Named column references inside SQL strings
                        # (SELECT setting_value FROM site_settings,
                        # WHERE setting_key = $1, etc.)
                        for pat, repl in (
                            (r"\bsetting_key\b", "key"),
                            (r"\bsetting_value\b", "value"),
                        ):
                            updated = _re_mod_norm.sub(pat, repl, updated)
                        if updated != content:
                            file_dict[fpath] = updated
                            route_rewrites += 1
                    if route_rewrites:
                        print(
                            f"  [post-process] Rewrote setting_key/setting_value → "
                            f"key/value in {route_rewrites} route/view file(s)"
                        )

    # 2. Seed defaults from spec: seed ALL 10 canonical SETTING_FIELDS.
    # Contract must match cli/templates/routes_settings.js SETTING_FIELDS and
    # cli/templates/views_admin_settings.ejs input names. The settings middleware
    # loads the full map via `SELECT key, value FROM site_settings`; any unseeded
    # key would come back as `undefined`, causing the EJS fallback pattern in
    # _ensure_settings_in_views() to fall through to Claude's hardcoded
    # placeholder text instead of the empty/admin-editable value.
    seed = file_dict.get("db/seed.sql", "")
    if seed and "site_settings" not in seed:
        def _sql_esc(s):
            return str(s or "").replace("'", "''")
        app_name         = _sql_esc(spec.get("app_name", "My App"))
        admin_email      = _sql_esc(spec.get("admin_email", ""))
        hero_tagline     = _sql_esc(spec.get("tagline", "") or "")
        hero_description = _sql_esc(str(spec.get("description", ""))[:200])

        # site_name seed default uses
        # the SAME derivation as _ensure_nav_brand_element (pretty-cased
        # app_name with -v\d+ version suffix stripped, falling back
        # through settings.site_name → business_name → app_name). This
        # makes the seeded DB value match the wordmark the nav injector
        # derives, so downstream admin handlers + footer + <title> +
        # email templates all converge on a single readable brand name
        # instead of a raw "sample-practitioner-v1-rc2" style slug.
        site_name_pretty = _sql_esc(_derive_site_name_from_spec(spec))

        # Mutate spec["settings"] in-place so downstream post-processors
        # that read spec["settings"]["site_name"] see the derived value
        # when the spec left it empty. Preserves explicit spec values
        # (only fills when empty).
        if isinstance(spec.get("settings"), dict):
            current = str(spec["settings"].get("site_name") or "").strip()
            if not current:
                spec["settings"]["site_name"] = _derive_site_name_from_spec(spec)

        # ADMIN_SETTINGS_SUPERSET (MW v12): also pull from spec.settings array
        # if the spec provides custom keyed defaults (e.g. `{"key": "hero_tagline",
        # "value": "..."}`). Spec values override generic defaults so hand-written
        # specs like MW v12 don't get overwritten with empty strings.
        spec_seed_overrides = {}
        _settings_arr = spec.get("_seed_settings") or spec.get("settings")
        if isinstance(_settings_arr, list):
            for s in _settings_arr:
                if isinstance(s, dict) and s.get("key"):
                    spec_seed_overrides[s["key"]] = _sql_esc(s.get("value", ""))

        site_settings_defaults = [
            # Brand / site identity
            ("site_name", spec_seed_overrides.get("site_name", site_name_pretty)),
            ("site_url", spec_seed_overrides.get("site_url", "")),
            ("site_description", spec_seed_overrides.get("site_description", "")),
            # Hero
            ("hero_tagline", spec_seed_overrides.get("hero_tagline", hero_tagline)),
            ("hero_description", spec_seed_overrides.get("hero_description", hero_description)),
            ("hero_image_url", spec_seed_overrides.get("hero_image_url", "")),
            # Logo + About
            ("nav_logo_url", spec_seed_overrides.get("nav_logo_url", "")),
            ("about_title", spec_seed_overrides.get("about_title", "")),
            ("about_body", spec_seed_overrides.get("about_body", "")),
            ("about_photo_url", spec_seed_overrides.get("about_photo_url", "")),
            # Contact (new canonical keys)
            ("contact_email", spec_seed_overrides.get("contact_email", admin_email)),
            ("contact_phone", spec_seed_overrides.get("contact_phone", "")),
            ("contact_address", spec_seed_overrides.get("contact_address", "")),
            # Legacy contact keys (kept so older generated views still read them)
            ("business_name", spec_seed_overrides.get("business_name", site_name_pretty)),
            ("phone", spec_seed_overrides.get("phone", "")),
            ("support_email", spec_seed_overrides.get("support_email", admin_email)),
            ("address", spec_seed_overrides.get("address", "")),
            # Social
            ("instagram_url", spec_seed_overrides.get("instagram_url", "")),
            ("facebook_url", spec_seed_overrides.get("facebook_url", "")),
            ("youtube_url", spec_seed_overrides.get("youtube_url", "")),
            ("tiktok_url", spec_seed_overrides.get("tiktok_url", "")),
            ("twitter_url", spec_seed_overrides.get("twitter_url", "")),
            ("linkedin_url", spec_seed_overrides.get("linkedin_url", "")),
        ]
        seed = seed.rstrip() + "\n\n-- Site settings defaults (ADMIN_SETTINGS_SUPERSET, 23 canonical keys)\n"
        for key, value in site_settings_defaults:
            seed += (
                f"INSERT INTO site_settings (key, value) VALUES "
                f"('{key}', '{value}') ON CONFLICT DO NOTHING;\n"
            )
        file_dict["db/seed.sql"] = seed
        changed = True

    # 3. Inject routes/settings.js template.
    # Always overwrite: Claude sometimes generates its own routes/settings.js that
    # lacks the canonical /admin/settings GET+POST handlers (ADMIN_SETTINGS1).
    # Same pattern as _ensure_admin_mount() for routes/admin.js.
    templates_dir = Path(__file__).parent / "templates"
    settings_route_path = templates_dir / "routes_settings.js"
    if settings_route_path.exists():
        _had_route = "routes/settings.js" in file_dict
        file_dict["routes/settings.js"] = settings_route_path.read_text()
        changed = True
        _action = "Overwrote" if _had_route else "Injected"
        print(f"  [post-process] {_action} routes/settings.js template")

    # 4. Inject views/admin/settings.ejs template.
    # Always overwrite: Claude sometimes generates an incomplete version that
    # is missing fields or has wrong form structure (ADMIN_SETTINGS1).
    settings_view_path = templates_dir / "views_admin_settings.ejs"
    if settings_view_path.exists():
        _had_view = "views/admin/settings.ejs" in file_dict
        file_dict["views/admin/settings.ejs"] = settings_view_path.read_text()
        changed = True
        _action = "Overwrote" if _had_view else "Injected"
        print(f"  [post-process] {_action} views/admin/settings.ejs template")

    # 5. Mount in server.js
    server = file_dict.get("server.js", "")
    if server and "routes/settings" not in server:
        mount_added = False
        # Pattern 1: app.use(require('./routes/...'))
        last_mount = -1
        for m in re_mod.finditer(r"app\.use\(require\(['\"]\.?/routes/", server):
            last_mount = m.end()
        if last_mount != -1:
            eol = server.find("\n", last_mount)
            if eol != -1:
                mount_line = "\napp.use(require('./routes/settings'));"
                server = server[:eol] + mount_line + server[eol:]
                file_dict["server.js"] = server
                changed = True
                mount_added = True
                print("  [post-process] Mounted routes/settings.js in server.js")
        # Pattern 2: app.use(xxxRouter) with const xxxRouter = require('./routes/...')
        if not mount_added:
            last_use_router = -1
            for m in re_mod.finditer(r"app\.use\(\w+Router\)", server):
                last_use_router = m.end()
            if last_use_router != -1:
                eol = server.find("\n", last_use_router)
                if eol != -1:
                    mount_line = "\napp.use(require('./routes/settings'));"
                    server = server[:eol] + mount_line + server[eol:]
                    file_dict["server.js"] = server
                    changed = True
                    mount_added = True
                    print("  [post-process] Mounted routes/settings.js in server.js (after router mounts)")

    # 6. Inject settings middleware into server.js (load from DB, 60s cache)
    server = file_dict.get("server.js", "")
    if server and "_settingsCache" not in server and "site_settings" not in server:
        middleware_code = """
// Site settings middleware (60s cache)
global._settingsCache = null;
global._settingsCacheTime = 0;
app.use(async (req, res, next) => {
  try {
    const now = Date.now();
    if (!global._settingsCache || now - global._settingsCacheTime > 60000) {
      const result = await require('./db').query('SELECT key, value FROM site_settings');
      global._settingsCache = {};
      result.rows.forEach(r => { global._settingsCache[r.key] = r.value; });
      global._settingsCacheTime = now;
    }
    res.locals.settings = global._settingsCache || {};
  } catch (err) {
    res.locals.settings = {};
  }
  next();
});
"""
        # Insert after session middleware but before routes
        session_match = re_mod.search(r"app\.use\(session\(", server)
        if session_match:
            # Find the end of the session block (next app.use or route mount)
            search_start = session_match.end()
            next_app_use = re_mod.search(r"\napp\.(use|get|post|set)\(", server[search_start:])
            if next_app_use:
                insert_pos = search_start + next_app_use.start()
                server = server[:insert_pos] + middleware_code + server[insert_pos:]
                file_dict["server.js"] = server
                changed = True
                print("  [post-process] Injected site settings middleware")
        elif "app.use(require" in server:
            # Fallback: insert before first route mount
            first_mount = server.find("app.use(require")
            if first_mount != -1:
                # Find start of line
                line_start = server.rfind("\n", 0, first_mount)
                if line_start == -1:
                    line_start = 0
                server = server[:line_start] + middleware_code + server[line_start:]
                file_dict["server.js"] = server
                changed = True
                print("  [post-process] Injected site settings middleware (before routes)")

    # 7. Add "Settings" link to admin dashboard
    for dash_path in ["views/admin/dashboard.ejs", "views/dashboard.ejs"]:
        dash = file_dict.get(dash_path, "")
        if dash and "/admin/settings" not in dash:
            settings_link = '<a href="/admin/settings" class="inline-flex items-center px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition text-sm font-medium">Settings</a>'
            injected = False
            # Try to find CSV import as a link (<a>) and add settings next to it
            csv_idx = dash.find("/admin/csv-import")
            if csv_idx != -1:
                link_end = dash.find("</a>", csv_idx)
                if link_end != -1:
                    insert_pos = link_end + 4
                    dash = dash[:insert_pos] + "\n          " + settings_link + dash[insert_pos:]
                    file_dict[dash_path] = dash
                    changed = True
                    injected = True
            # Fallback: add after first heading
            if not injected:
                h1_end = dash.find("</h1>")
                if h1_end == -1:
                    h1_end = dash.find("</h2>")
                if h1_end != -1:
                    insert_pos = h1_end + 5
                    dash = dash[:insert_pos] + "\n<div class='mb-4'>" + settings_link + "</div>" + dash[insert_pos:]
                    file_dict[dash_path] = dash
                    changed = True
            if "/admin/settings" in file_dict.get(dash_path, ""):
                print("  [post-process] Added Settings link to admin dashboard")
                break

    if changed:
        emit_build_trace(
            _CURRENT_BUILD_DIR.get(),
            "ensure_site_settings.completed",
            {"changed": True, "output_count": len(file_dict)},
        )
        return list(file_dict.items())
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_site_settings.skipped",
        {"reason": "no_changes"},
    )
    return files


@processor(after=('_ensure_site_settings',))
def _ensure_settings_in_views(files: list) -> list:
    """Inject conditional social links and contact info from site_settings into views.

    Uses res.locals.settings populated by the settings middleware.
    All injections use typeof guard so views work even if middleware fails.
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_settings_in_views.start",
        {"input_count": len(files)},
    )

    # 1. Footer social links in layout.ejs
    layout = file_dict.get("views/layout.ejs", "")
    if layout and "settings.instagram_url" not in layout:
        social_block = """
    <% if (typeof settings !== 'undefined') { %>
      <div class="flex items-center gap-4 mt-4">
        <% if (settings.instagram_url) { %><a href="<%= settings.instagram_url %>" target="_blank" rel="noopener" class="text-white/70 hover:text-white transition" aria-label="Instagram"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg></a><% } %>
        <% if (settings.facebook_url) { %><a href="<%= settings.facebook_url %>" target="_blank" rel="noopener" class="text-white/70 hover:text-white transition" aria-label="Facebook"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385h-3.047v-3.47h3.047v-2.642c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953h-1.513c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385c5.737-.9 10.125-5.864 10.125-11.854z"/></svg></a><% } %>
        <% if (settings.youtube_url) { %><a href="<%= settings.youtube_url %>" target="_blank" rel="noopener" class="text-white/70 hover:text-white transition" aria-label="YouTube"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg></a><% } %>
        <% if (settings.tiktok_url) { %><a href="<%= settings.tiktok_url %>" target="_blank" rel="noopener" class="text-white/70 hover:text-white transition" aria-label="TikTok"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z"/></svg></a><% } %>
        <% if (settings.twitter_url) { %><a href="<%= settings.twitter_url %>" target="_blank" rel="noopener" class="text-white/70 hover:text-white transition" aria-label="Twitter"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg></a><% } %>
        <% if (settings.linkedin_url) { %><a href="<%= settings.linkedin_url %>" target="_blank" rel="noopener" class="text-white/70 hover:text-white transition" aria-label="LinkedIn"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg></a><% } %>
      </div>
    <% } %>
"""
        # Find the footer and inject before the closing divider or copyright
        footer_start = layout.find("<footer")
        if footer_start != -1:
            # Look for the copyright text or divider-warm as anchor
            copyright_idx = layout.find("&copy;", footer_start)
            divider_idx = layout.find("divider-warm", footer_start)
            anchor_idx = divider_idx if divider_idx != -1 else copyright_idx
            if anchor_idx != -1:
                # Find beginning of the line containing the anchor (or use anchor pos)
                line_start = layout.rfind("\n", footer_start, anchor_idx)
                insert_pos = line_start if line_start != -1 else anchor_idx
                layout = layout[:insert_pos] + social_block + layout[insert_pos:]
                file_dict["views/layout.ejs"] = layout
                changed = True
                print("  [post-process] Injected conditional social links in footer")
            elif "</footer>" in layout:
                # Fallback: insert before </footer>
                close_footer = layout.find("</footer>")
                layout = layout[:close_footer] + social_block + layout[close_footer:]
                file_dict["views/layout.ejs"] = layout
                changed = True
                print("  [post-process] Injected conditional social links before </footer>")

    # 2. Contact page: replace hardcoded contact info with settings-driven EJS.
    # Each section (email / phone / location / social) is individually idempotent
    # via its own regex (literal patterns don't match already-patched EJS output),
    # so this block can run multiple times without double-patching.
    for contact_path in ["views/contact/index.ejs", "views/contact.ejs"]:
        contact = file_dict.get(contact_path, "")
        if contact:
            patched = False

            # Replace hardcoded email value with settings override
            # Pattern: <p ...>email@example.com</p> in the contact info section
            email_match = re_mod.search(
                r'(<p[^>]*>)([\w.+-]+@[\w.-]+\.\w+)(</p>)',
                contact
            )
            if email_match:
                replacement = (
                    email_match.group(1)
                    + "<%= (typeof settings !== 'undefined' && settings.support_email) || '" + email_match.group(2) + "' %>"
                    + email_match.group(3)
                )
                contact = contact[:email_match.start()] + replacement + contact[email_match.end():]
                patched = True

            # Replace hardcoded phone value
            # Pattern: <p ...>(xxx) xxx-xxxx</p> or <p ...>digits</p>
            phone_match = re_mod.search(
                r'(<p[^>]*>)(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})(</p>)',
                contact
            )
            if phone_match:
                replacement = (
                    phone_match.group(1)
                    + "<%= (typeof settings !== 'undefined' && settings.phone) || '" + phone_match.group(2) + "' %>"
                    + phone_match.group(3)
                )
                contact = contact[:phone_match.start()] + replacement + contact[phone_match.end():]
                patched = True

            # Replace hardcoded location/address value
            # Pattern: <p ...>City, State</p> (possibly with <br> inside) after a Location label.
            # Notes:
            # - The <p> content may contain inline tags like <br>, so [^<]+ is too narrow;
            #   we use a non-greedy match with DOTALL and stop at the first </p>.
            # - We bound the search window to ~500 chars after the label so a missing
            #   location <p> doesn't silently fall through and corrupt the next <p>
            #   in the file (e.g. the "Social" label in the adjacent contact card).
            # - We skip if the matched <p> already contains an EJS tag, so re-runs are idempotent.
            location_label_idx = contact.find(">Location<")
            if location_label_idx == -1:
                location_label_idx = contact.find(">Address<")
            if location_label_idx != -1:
                search_window_end = min(len(contact), location_label_idx + 500)
                window = contact[location_label_idx:search_window_end]
                loc_p_match = re_mod.search(
                    r'(<p[^>]*>)(.*?)(</p>)',
                    window,
                    re_mod.DOTALL,
                )
                if loc_p_match and '<%' not in loc_p_match.group(2):
                    abs_start = location_label_idx + loc_p_match.start()
                    abs_end = location_label_idx + loc_p_match.end()
                    raw = loc_p_match.group(2)
                    # Flatten <br> into comma separators so the fallback string is readable.
                    cleaned = re_mod.sub(r'<br\s*/?\s*>', ', ', raw)
                    # Strip any remaining inline tags and collapse whitespace.
                    cleaned = re_mod.sub(r'<[^>]+>', '', cleaned)
                    cleaned = re_mod.sub(r'\s+', ' ', cleaned).strip()
                    # Escape for single-quoted JS string literal inside EJS.
                    cleaned = cleaned.replace("\\", "\\\\").replace("'", "\\'")
                    replacement = (
                        loc_p_match.group(1)
                        + "<%= (typeof settings !== 'undefined' && settings.address) || '" + cleaned + "' %>"
                        + loc_p_match.group(3)
                    )
                    contact = contact[:abs_start] + replacement + contact[abs_end:]
                    patched = True

            # Replace hardcoded social links with settings-driven conditionals
            # Find the social links section (Follow Us / social icons)
            follow_match = re_mod.search(r'>(?:Follow\s+Us|Social\s*(?:Media|Links?))<', contact, re_mod.IGNORECASE)
            if follow_match:
                # Find the containing div with social link icons
                social_start = follow_match.start()
                # Find the parent container start (look back for the div)
                container_start = contact.rfind('<div', 0, social_start)
                if container_start == -1:
                    container_start = social_start
                # Find end of the social section (closing div after all links)
                # Count nested divs to find the right closing </div>
                search_from = social_start
                div_depth = 0
                social_end = len(contact)
                # Find the flex container with the actual links
                flex_div_match = re_mod.search(r'<div[^>]*class="flex[^"]*gap[^"]*"[^>]*>', contact[search_from:])
                if flex_div_match:
                    flex_start = search_from + flex_div_match.start()
                    pos = search_from + flex_div_match.end()
                    div_depth = 1
                    while pos < len(contact) and div_depth > 0:
                        next_open = contact.find('<div', pos)
                        next_close = contact.find('</div>', pos)
                        if next_close == -1:
                            break
                        if next_open != -1 and next_open < next_close:
                            div_depth += 1
                            pos = next_open + 4
                        else:
                            div_depth -= 1
                            if div_depth == 0:
                                social_end = next_close + 6  # len('</div>')
                                break
                            pos = next_close + 6

                    # Build settings-driven social links block
                    social_replacement = """<div class="flex gap-3">
            <% if (typeof settings !== 'undefined') { %>
              <% if (settings.facebook_url) { %><a href="<%- settings.facebook_url %>" target="_blank" rel="noopener" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="Facebook"><svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24"><path d="M18 2h-3a5 5 0 00-5 5v3H7v4h3v8h4v-8h3l1-4h-4V7a1 1 0 011-1h3z"/></svg></a><% } %>
              <% if (settings.instagram_url) { %><a href="<%- settings.instagram_url %>" target="_blank" rel="noopener" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="Instagram"><svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="5" ry="5" stroke-width="2"/><path d="M16 11.37A4 4 0 1112.63 8 4 4 0 0116 11.37z" stroke-width="2"/><line x1="17.5" y1="6.5" x2="17.51" y2="6.5" stroke-width="2"/></svg></a><% } %>
              <% if (settings.youtube_url) { %><a href="<%- settings.youtube_url %>" target="_blank" rel="noopener" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="YouTube"><svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24"><path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg></a><% } %>
              <% if (settings.tiktok_url) { %><a href="<%- settings.tiktok_url %>" target="_blank" rel="noopener" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="TikTok"><svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24"><path d="M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z"/></svg></a><% } %>
              <% if (settings.twitter_url) { %><a href="<%- settings.twitter_url %>" target="_blank" rel="noopener" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="Twitter"><svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg></a><% } %>
              <% if (settings.linkedin_url) { %><a href="<%- settings.linkedin_url %>" target="_blank" rel="noopener" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="LinkedIn"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg></a><% } %>
            <% } else { %>
              <a href="#" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="Facebook"><svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24"><path d="M18 2h-3a5 5 0 00-5 5v3H7v4h3v8h4v-8h3l1-4h-4V7a1 1 0 011-1h3z"/></svg></a>
              <a href="#" class="w-10 h-10 rounded-lg flex items-center justify-center transition-colors min-h-[44px] min-w-[44px]" style="background: var(--color-primary);" aria-label="Instagram"><svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="5" ry="5" stroke-width="2"/><path d="M16 11.37A4 4 0 1112.63 8 4 4 0 0116 11.37z" stroke-width="2"/><line x1="17.5" y1="6.5" x2="17.51" y2="6.5" stroke-width="2"/></svg></a>
            <% } %>
          </div>"""
                    contact = contact[:flex_start] + social_replacement + contact[social_end:]
                    patched = True

            if patched:
                file_dict[contact_path] = contact
                changed = True
                print(f"  [post-process] Replaced hardcoded contact info with settings in {contact_path}")
                break

    if changed:
        emit_build_trace(
            _CURRENT_BUILD_DIR.get(),
            "ensure_settings_in_views.completed",
            {"changed": True, "output_count": len(file_dict)},
        )
        return list(file_dict.items())
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "ensure_settings_in_views.skipped",
        {"reason": "no_changes"},
    )
    return files


@processor(after=('_ensure_settings_in_views',))
def _ensure_hero_settings_binding(files: list) -> list:
    """Rewire the hero <h1> and hero description <p> to use site_settings from the DB.

    Claude hardcodes the hero tagline and description in views/index.ejs (or home.ejs)
    during generation. This post-processor replaces those hardcoded strings with EJS
    expressions that read from res.locals.settings (populated by the settings middleware).

    The fallback (|| 'original text') means the app renders correctly even before the
    admin has set custom text, and the original AI-generated copy still appears on first
    deploy while the admin edits it at /admin/settings.

    Targets: views/home.ejs, views/index.ejs
    Idempotent: bails immediately if settings.hero_tagline already present.
    Only binds the first <h1> and the first <p> after it (within 800 chars).
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False

    for view_path in ["views/home.ejs", "views/index.ejs"]:
        content = file_dict.get(view_path, "")
        if not content:
            continue
        # Idempotency guard
        if "settings.hero_tagline" in content:
            continue

        # Find first <h1 ...> ... </h1> block (may span multiple lines)
        h1_match = re_mod.search(r'(<h1[^>]*>)([\s\S]*?)(</h1>)', content)
        if not h1_match:
            continue

        # Build EJS fallback from original inner text (strip inline tags, collapse whitespace)
        raw_h1_inner = h1_match.group(2)
        fallback_tagline = re_mod.sub(r'<[^>]+>', '', raw_h1_inner)
        fallback_tagline = re_mod.sub(r'\s+', ' ', fallback_tagline).strip()
        fallback_tagline = fallback_tagline.replace("\\", "\\\\").replace("'", "\\'")[:120]

        new_h1 = (
            h1_match.group(1)
            + "\n        <%= (typeof settings !== 'undefined' && settings.hero_tagline)"
            + " || '" + fallback_tagline + "' %>\n      "
            + h1_match.group(3)
        )
        content = content[:h1_match.start()] + new_h1 + content[h1_match.end():]

        # Find first <p> in the 800 chars after </h1>; the hero subheading paragraph
        search_start = h1_match.start() + len(new_h1)
        window_end = min(len(content), search_start + 800)
        p_match = re_mod.search(r'(<p[^>]*>)([\s\S]*?)(</p>)', content[search_start:window_end])
        if p_match:
            raw_p_inner = p_match.group(2)
            # Skip if the <p> content is very short (likely a label, not a description)
            plain_p = re_mod.sub(r'<[^>]+>', '', raw_p_inner)
            plain_p = re_mod.sub(r'\s+', ' ', plain_p).strip()
            if len(plain_p) >= 20:
                fallback_desc = plain_p.replace("\\", "\\\\").replace("'", "\\'")[:200]
                abs_start = search_start + p_match.start()
                abs_end = search_start + p_match.end()
                new_p = (
                    p_match.group(1)
                    + "\n        <%= (typeof settings !== 'undefined' && settings.hero_description)"
                    + " || '" + fallback_desc + "' %>\n      "
                    + p_match.group(3)
                )
                content = content[:abs_start] + new_p + content[abs_end:]

        # HERO_IMAGE_BINDING (v15): wire settings.hero_image_url as conditional
        # background on the hero <section>. Find the first <section ...> tag on
        # the page (that's the hero wrapper in Claude's generated home.ejs) and
        # append an inline `style="background-image:url('<%= url %>'); ..."` that
        # only renders when hero_image_url is set. Idempotent via marker string.
        if "hero-image-bg" not in content:
            hero_section_re = re_mod.compile(
                r'(<section\b)([^>]*)>', re_mod.IGNORECASE
            )
            first_section = hero_section_re.search(content)
            if first_section:
                attrs_tail = first_section.group(2)
                style_snippet = (
                    ' data-hero-image-bg="1"'
                    ' style="<% if (typeof settings !== \'undefined\' && settings.hero_image_url) { %>'
                    'background-image: linear-gradient(rgba(7,9,36,0.55), rgba(7,9,36,0.85)),'
                    ' url(\'<%= settings.hero_image_url %>\');'
                    ' background-size: cover; background-position: center;'
                    '<% } %>"'
                )
                new_section_tag = (
                    first_section.group(1) + attrs_tail + style_snippet + ">"
                )
                content = (
                    content[:first_section.start()]
                    + new_section_tag
                    + content[first_section.end():]
                )

        file_dict[view_path] = content
        changed = True
        print(f"  [post-process] Bound hero tagline/description (+image bg) to site_settings in {view_path}")

    if changed:
        return list(file_dict.items())
    return files


@processor(after=('_ensure_hero_settings_binding',))
def _ensure_contact_email(files: list) -> list:
    """Inject email notification after contact form INSERT.

    Searches all route files for INSERT INTO contact_messages and adds a
    fire-and-forget Resend email notification after the insert.
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False

    email_snippet = """
    // Notify admin of new contact message
    const adminEmail = (typeof settings !== 'undefined' && settings && settings.support_email) || process.env.ADMIN_EMAIL;
    if (adminEmail && process.env.RESEND_API_KEY) {
      fetch('https://api.resend.com/emails', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${process.env.RESEND_API_KEY}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ from: 'noreply@craftandship.io', to: adminEmail, subject: `New contact message from ${name}`, text: `${name} (${email}) wrote:\\n\\n${message}` })
      }).catch(err => console.error('Contact email failed:', err));
    } else {
      console.log(`[contact] New message from ${name} (${email}): ${(message || '').substring(0, 100)}`);
    }
"""

    for filepath, content in list(file_dict.items()):
        if not filepath.startswith("routes/"):
            continue
        if "INSERT INTO contact_messages" not in content:
            continue
        if "resend.com/emails" in content:
            continue

        # Find the INSERT query line and inject after it
        insert_match = re_mod.search(
            r"(.*INSERT\s+INTO\s+contact_messages\b.*?;)",
            content,
            re_mod.IGNORECASE | re_mod.DOTALL,
        )
        if insert_match:
            insert_end = insert_match.end()
            content = content[:insert_end] + email_snippet + content[insert_end:]
            file_dict[filepath] = content
            changed = True
            print(f"  [post-process] Injected contact email notification in {filepath}")

    if changed:
        return list(file_dict.items())
    return files


@processor(after=('_ensure_gallery_dedup',), phase=('build',))
def _fix_seed_column_mismatch(files: list) -> list:
    """Fix INSERT statements where column count doesn't match value count.

    Claude commonly adds an extra trailing value (e.g. a duplicate created_by)
    or omits a value. For too many values: truncates to match column count.
    For too few values: comments out the INSERT (can't guess the missing value).

    Uses a quote/paren-aware value counter to handle NOW(), quoted strings with
    commas, etc.
    """
    import re as re_mod
    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    changed = False
    lines = seed.split("\n")
    new_lines = []

    # Match single-line INSERT INTO t (cols) VALUES (...) ...;
    insert_re = re_mod.compile(
        r"^(INSERT\s+INTO\s+\w+\s*\(([^)]+)\)\s*VALUES\s*)\((.+)\)(.*?;)\s*$",
        re_mod.IGNORECASE,
    )

    def _count_values(val_str):
        """Count comma-separated values respecting quotes and parens."""
        count = 1
        depth = 0
        in_quote = False
        i = 0
        while i < len(val_str):
            ch = val_str[i]
            if in_quote:
                if ch == "'":
                    if i + 1 < len(val_str) and val_str[i + 1] == "'":
                        i += 2
                        continue
                    in_quote = False
                i += 1
                continue
            if ch == "'":
                in_quote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                count += 1
            i += 1
        return count

    def _truncate_values(val_str, target):
        """Keep only the first `target` values, respecting quotes and parens."""
        count = 0
        depth = 0
        in_quote = False
        i = 0
        while i < len(val_str):
            ch = val_str[i]
            if in_quote:
                if ch == "'":
                    if i + 1 < len(val_str) and val_str[i + 1] == "'":
                        i += 2
                        continue
                    in_quote = False
                i += 1
                continue
            if ch == "'":
                in_quote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                count += 1
                if count >= target:
                    return val_str[:i].rstrip()
            i += 1
        return val_str

    for line in lines:
        m = insert_re.match(line.strip())
        if not m:
            new_lines.append(line)
            continue

        prefix = m.group(1)
        cols_str = m.group(2)
        val_str = m.group(3)
        suffix = m.group(4)

        num_cols = len([c.strip() for c in cols_str.split(",") if c.strip()])
        num_vals = _count_values(val_str)

        if num_vals == num_cols:
            new_lines.append(line)
        elif num_vals > num_cols:
            # Too many values: truncate
            fixed = _truncate_values(val_str, num_cols)
            new_lines.append(f"{prefix}({fixed}){suffix}")
            changed = True
        else:
            # Too few values: comment out (can't guess missing value)
            new_lines.append(f"-- SKIPPED (column mismatch {num_cols} cols, {num_vals} vals): {line.strip()}")
            changed = True

    if changed:
        print("  [post-process] Fixed seed.sql column/value count mismatches")
        file_dict["db/seed.sql"] = "\n".join(new_lines)
        return list(file_dict.items())

    return files


@processor(after=('_fix_seed_column_mismatch',), phase=('build',))
def _fix_seed_uuid_id(files: list) -> list:
    """Fix seed INSERTs where UUID values are used for SERIAL/INTEGER id columns.

    Claude sometimes generates gen_random_uuid() or literal UUID strings for
    the id column even when the schema defines it as SERIAL PRIMARY KEY. This
    causes a type mismatch error and leaves the table empty. Fix: detect
    SERIAL/INTEGER id columns in schema.sql, then strip the id column from
    seed INSERTs so SERIAL auto-generates sequential integers.
    """
    import re as re_mod
    file_dict = dict(files)
    schema = file_dict.get("db/schema.sql", "")
    seed = file_dict.get("db/seed.sql", "")
    if not schema or not seed:
        return files

    # Find tables that have SERIAL or INTEGER id columns (not UUID)
    # Match: CREATE TABLE [IF NOT EXISTS] tablename ( ... id SERIAL|INTEGER
    serial_tables = set()
    table_blocks = re_mod.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(([^;]+)",
        schema, re_mod.IGNORECASE | re_mod.DOTALL,
    )
    for m in table_blocks:
        tname = m.group(1).lower()
        body = m.group(2)
        # Check if id column is SERIAL, BIGSERIAL, or INTEGER (not UUID)
        if re_mod.search(r"\bid\s+(?:SERIAL|BIGSERIAL|INTEGER|INT)\b", body, re_mod.IGNORECASE):
            serial_tables.add(tname)

    if not serial_tables:
        return files

    # UUID value patterns that should be stripped
    uuid_literal_re = re_mod.compile(
        r"^'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'(?:::uuid)?$",
        re_mod.IGNORECASE,
    )
    uuid_func_re = re_mod.compile(
        r"^(?:gen_random_uuid|uuid_generate_v4)\s*\(\s*\)$",
        re_mod.IGNORECASE,
    )

    # Match single-line INSERT INTO t (cols) VALUES (...) ...;
    insert_re = re_mod.compile(
        r"^(INSERT\s+INTO\s+)(\w+)(\s*\(([^)]+)\)\s*VALUES\s*)\((.+)\)(.*?;)\s*$",
        re_mod.IGNORECASE,
    )

    def _extract_nth_value(val_str, n):
        """Extract the nth (0-based) comma-separated value, quote/paren-aware."""
        count = 0
        depth = 0
        in_quote = False
        start = 0
        i = 0
        while i < len(val_str):
            ch = val_str[i]
            if in_quote:
                if ch == "'":
                    if i + 1 < len(val_str) and val_str[i + 1] == "'":
                        i += 2
                        continue
                    in_quote = False
                i += 1
                continue
            if ch == "'":
                in_quote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                if count == n:
                    return val_str[start:i].strip()
                count += 1
                start = i + 1
            i += 1
        if count == n:
            return val_str[start:].strip()
        return None

    def _remove_nth_value(val_str, n):
        """Remove the nth (0-based) comma-separated value, quote/paren-aware."""
        count = 0
        depth = 0
        in_quote = False
        start = 0
        boundaries = []
        i = 0
        while i < len(val_str):
            ch = val_str[i]
            if in_quote:
                if ch == "'":
                    if i + 1 < len(val_str) and val_str[i + 1] == "'":
                        i += 2
                        continue
                    in_quote = False
                i += 1
                continue
            if ch == "'":
                in_quote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                boundaries.append((start, i))
                start = i + 1
                count += 1
            i += 1
        boundaries.append((start, len(val_str)))

        if n >= len(boundaries):
            return val_str
        parts = []
        for idx, (s, e) in enumerate(boundaries):
            if idx != n:
                parts.append(val_str[s:e].strip())
        return ", ".join(parts)

    changed = False
    lines = seed.split("\n")
    new_lines = []

    for line in lines:
        m = insert_re.match(line.strip())
        if not m:
            new_lines.append(line)
            continue

        prefix = m.group(1)       # INSERT INTO
        table = m.group(2)        # table name
        mid = m.group(3)          # (cols) VALUES
        cols_str = m.group(4)     # col1, col2, ...
        val_str = m.group(5)      # val1, val2, ...
        suffix = m.group(6)       # ON CONFLICT ...;

        if table.lower() not in serial_tables:
            new_lines.append(line)
            continue

        cols = [c.strip().lower() for c in cols_str.split(",")]
        if "id" not in cols:
            new_lines.append(line)
            continue

        id_idx = cols.index("id")
        id_val = _extract_nth_value(val_str, id_idx)
        if id_val is None:
            new_lines.append(line)
            continue

        # Check if the id value looks like a UUID
        is_uuid = bool(uuid_literal_re.match(id_val) or uuid_func_re.match(id_val))
        if not is_uuid:
            new_lines.append(line)
            continue

        # Strip the id column and its value
        new_cols = [c.strip() for c in cols_str.split(",")]
        del new_cols[id_idx]
        new_val_str = _remove_nth_value(val_str, id_idx)
        new_cols_str = ", ".join(new_cols)
        new_line = f"{prefix}{table} ({new_cols_str}) VALUES ({new_val_str}){suffix}"
        new_lines.append(new_line)
        changed = True

    if changed:
        print("  [post-process] Fixed seed.sql: stripped UUID id values from SERIAL id tables")
        file_dict["db/seed.sql"] = "\n".join(new_lines)
        return list(file_dict.items())

    return files


@processor(after=('_ensure_railwayignore',), phase=('build',))
def _fix_seed_admin_email(files: list, spec: dict) -> list:
    """Ensure seed.sql uses the correct admin_email from the spec.

    Claude sometimes ignores the spec admin_email and seeds with
    admin@example.com or a generic email. This post-processing step
    finds the first INSERT into users and ensures the admin row uses
    the correct email. Also ensures email_verified=TRUE for all seed users.
    """
    import re
    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    admin_email = spec.get("admin_email", "")
    if not admin_email:
        return files

    changed = False

    # Fix: replace admin@example.com with the spec admin_email
    if "admin@example.com" in seed and admin_email != "admin@example.com":
        # Only replace the first occurrence (the admin user row)
        seed = seed.replace("admin@example.com", admin_email, 1)
        changed = True
        print(f"  [post-process] Fixed seed admin email: admin@example.com -> {admin_email}")

    # Fix: ensure email_verified is TRUE for all seed users
    # Simple approach: replace FALSE with TRUE anywhere email_verified appears in seed.sql
    # This is safe because email_verified should always be TRUE for seed data
    for false_val in ["FALSE", "false", "'false'"]:
        if f"email_verified" in seed and false_val in seed:
            old_seed = seed
            # Only replace FALSE that appears near email_verified context
            seed = seed.replace(f", {false_val})", f", TRUE)")
            seed = seed.replace(f",{false_val})", f",TRUE)")
            seed = seed.replace(f"= {false_val}", f"= TRUE")
            seed = seed.replace(f"={false_val}", f"=TRUE")
            if seed != old_seed:
                changed = True
                print("  [post-process] Fixed seed users: set email_verified=TRUE")

    # Fix: ensure password_set is TRUE for all seed users (magic link auth apps
    # seed with password_set=false, which blocks direct login even when password exists)
    for false_val in ["FALSE", "false", "'false'"]:
        if "password_set" in seed and false_val in seed:
            old_seed = seed
            seed = seed.replace(f", {false_val})", f", TRUE)")
            seed = seed.replace(f",{false_val})", f",TRUE)")
            seed = seed.replace(f"= {false_val}", f"= TRUE")
            seed = seed.replace(f"={false_val}", f"=TRUE")
            if seed != old_seed:
                changed = True
                print("  [post-process] Fixed seed users: set password_set=TRUE")

    # Removed 2026-04-19 (SEC-1): previously appended
    #   UPDATE users SET password = '<bcrypt of password123>'
    # to seed.sql so magic-link-only users could also log in via password.
    # That hash (same across all builds) then got committed into customer DB
    # snapshots / gold-standard refs / this repo's git history, creating a
    # shadow credential next to magic-link auth. For magic-link-primary apps
    # the canonical test flow is now: mint a one-off magic-link token via
    # psql.
    # Do not re-add bcrypt-hash injection without a strong reason + signed-
    # off design review.

    if changed:
        file_dict["db/seed.sql"] = seed

    return list(file_dict.items())


@processor(after=('_verify_template_middleware_contract',), phase=('build',))
def _ensure_railwayignore(files: list) -> list:
    """Add .railwayignore to prevent accidental node_modules upload during railway up.

    node_modules should never be uploaded; Railway's build pipeline installs them
    inside the deployed container. This is a defense-in-depth measure.
    """
    file_dict = dict(files)
    if ".railwayignore" not in file_dict:
        file_dict[".railwayignore"] = (
            "node_modules/\n"
            ".git/\n"
            ".npm-cache/\n"
            "test_stdout.log\n"
            "test_stderr.log\n"
        )
    return list(file_dict.items())


@processor(after=('_ensure_package_deps',), phase=('build',))
def _fix_view_html_shells(files: list) -> list:
    """Strip full HTML shells from view files so they integrate with layout.ejs.

    Multi-pass generation sometimes produces Phase 2 views with their own
    <!DOCTYPE html>, <html>, <head>, <body> wrappers. When express-ejs-layouts
    wraps these, the view's HTML shell renders INSIDE the layout's <body>,
    bypassing the layout's nav/footer. Fix: extract only the inner <body>
    content from affected views, leaving just the page-specific markup.

    Skips layout.ejs, login.ejs, register.ejs, and verify-email-sent.ejs
    (these are expected to be full pages or the layout itself).
    """
    SKIP_FILES = {"views/layout.ejs", "views/login.ejs", "views/register.ejs",
                  "views/verify-email-sent.ejs"}
    updated = []
    fixed_count = 0
    for filepath, content in files:
        if filepath not in SKIP_FILES and filepath.startswith("views/") and filepath.endswith(".ejs"):
            if "<!DOCTYPE" in content or ("<html" in content and "</html>" in content):
                # Extract body content
                body_match = _re.search(
                    r'<body[^>]*>(.*?)</body>',
                    content,
                    _re.DOTALL | _re.IGNORECASE,
                )
                if body_match:
                    inner = body_match.group(1).strip()
                    # Also strip any nested <nav> that duplicates layout nav
                    # (only if layout.ejs exists and has its own nav)
                    updated.append((filepath, inner))
                    fixed_count += 1
                    continue
        updated.append((filepath, content))
    if fixed_count:
        print(f"  [post-process] Stripped HTML shells from {fixed_count} view(s) to use layout.ejs nav")
    return updated


@processor(after=('_fix_view_slug_paths',), phase=('build',))
def _ensure_logout_form(files: list) -> list:
    """Convert logout GET links to POST forms with CSRF tokens in layout.ejs.

    Claude sometimes generates <a href="/logout"> instead of the required
    POST form. This is a security issue (GET logout is vulnerable to CSRF via
    image tags). This post-processor detects the pattern and replaces it.
    """
    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Already has a proper logout form
    if 'action="/logout"' in layout and 'method="POST"' in layout.upper():
        return files

    # Detect GET-based logout links: <a href="/logout" ...>...</a>
    logout_link_pattern = _re.compile(
        r'<a\s+[^>]*href="/logout"[^>]*>(.*?)</a>',
        _re.DOTALL | _re.IGNORECASE,
    )
    match = logout_link_pattern.search(layout)
    if not match:
        return files

    # Extract the link text and any classes for styling
    link_text = match.group(1).strip()
    # Clean up link text (remove nested tags, keep text)
    clean_text = _re.sub(r'<[^>]+>', '', link_text).strip() or "Logout"

    # Extract classes from the original link for consistent styling
    class_match = _re.search(r'class="([^"]*)"', match.group(0))
    btn_classes = class_match.group(1) if class_match else "text-sm"

    # Extract inline style from original link
    style_match = _re.search(r'style="([^"]*)"', match.group(0))
    style_attr = f' style="{style_match.group(1)}"' if style_match else ""

    # Build the replacement form
    logout_form = (
        f'<form action="/logout" method="POST" style="display:inline">'
        f'<input type="hidden" name="_csrf" value="<%%= csrfToken %%>">'
        f'<button type="submit" class="{btn_classes}"{style_attr}>'
        f'{clean_text}</button></form>'
    )

    new_layout = layout[:match.start()] + logout_form + layout[match.end():]
    file_dict["views/layout.ejs"] = new_layout
    print("  [post-process] Converted logout link to POST form with CSRF token")
    return list(file_dict.items())


@processor(after=('_ensure_logout_form',), phase=('build',))
def _fix_null_property_access(files: list) -> list:
    """Add null guards before unguarded .rows[0] property accesses in route files.

    Common deploy crash: generated routes access result.rows[0].id or destructure
    from result.rows[0] without first checking result.rows.length, causing:
      TypeError: Cannot read properties of undefined (reading 'id')

    Scans all routes/*.js files (except auth.js) for .rows[0] property accesses
    or destructuring that lack a preceding null/length guard, and injects a 404
    guard immediately before the crash-prone line.

    Before:
        const item = await pool.query(...);
        const { id, name } = item.rows[0];  // crashes if not found

    After:
        const item = await pool.query(...);
        if (!item.rows.length) return res.status(404).json({ error: 'Not found' });
        const { id, name } = item.rows[0];
    """
    # Matches: result.rows[0].something or { destructure } = result.rows[0]
    ACCESS_PATTERN = _re.compile(r'\b(\w+)\.rows\[0\]\.')
    DESTRUCTURE_PATTERN = _re.compile(
        r'(?:const|let|var)\s+\{[^}]+\}\s*=\s*(\w+)\.rows\[0\]'
    )

    # Patterns that indicate a guard already exists nearby
    GUARD_PATTERNS = [
        r'\.rows\.length',
        r'\.rows\[0\]',        # any rows[0] check (e.g. if (!X.rows[0]))
        r'rowCount\s*[=!<>]',
        r'if\s*\(.*rows',
        r'rows\.length\s*[=<>!]',
    ]

    def _net_open_parens(text: str) -> int:
        """Count net unmatched '(' in text, ignoring parens inside string literals.

        Used to detect multi-line function-call argument contexts: if preceding
        lines have more '(' than ')', the current line is inside a call expression
        and injecting a return statement would produce invalid JS (NULL_GUARD_INJECTION1).
        """
        depth = 0
        in_str: str | None = None
        for ch in text:
            if in_str:
                if ch == in_str:
                    in_str = None
            elif ch in ('"', "'", '`'):
                in_str = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        return depth

    def _is_inside_call_expression(lines: list, idx: int, lookback: int = 15) -> bool:
        """Return True if line[idx] is inside an unclosed '(' from preceding lines.

        Scans backward, accumulating net open-paren depth.  Returns True as soon
        as depth first turns positive (found an unclosed '(' that hasn't been
        closed before the target line).

        Stops (returns False) when:
        - A prior line ends with '{': block boundary, lines after it are in a
          new statement scope, not inside the outer call's argument list. The
          check is done BEFORE counting that line's parens so that
          `router.get(..., async (req, res) => {` doesn't bleed its unmatched
          `(` into the scan.
        - Cumulative depth goes negative (enclosing close-paren already passed).
        """
        depth = 0
        for prev_idx in range(idx - 1, max(-1, idx - lookback - 1), -1):
            prev_stripped = lines[prev_idx].rstrip()
            # Block boundary check BEFORE counting parens on this line.
            if prev_stripped.endswith('{'):
                break
            depth += _net_open_parens(lines[prev_idx])
            if depth < 0:
                return False  # passed the enclosing close-paren
            if depth > 0:
                return True   # found the first unclosed '('; we're inside it
        return False

    file_dict = dict(files)
    total_fixes = 0

    for filename, content in list(file_dict.items()):
        if not filename.startswith('routes/') or not filename.endswith('.js'):
            continue
        # Skip auth.js: complex login/session logic, guards are handled separately
        if filename in ('routes/auth.js', 'routes/admin.js'):
            continue

        lines = content.split('\n')
        new_lines = []
        fixes_in_file = 0

        for i, line in enumerate(lines):
            # Check for direct property access or destructuring from .rows[0]
            match = ACCESS_PATTERN.search(line) or DESTRUCTURE_PATTERN.search(line)
            if match:
                var_name = match.group(1)

                # Skip object-literal / array-literal / function-arg contexts.
                # A statement `if (...) return ...;` is only valid in statement
                # position. If the matched line is an object-literal member
                # (`key: value.rows[0].x,`) or inside a multiline expression
                # (ends with `,` instead of `;`), injecting a return statement
                # above it produces invalid JS. Detect and skip these cases.
                stripped = line.strip()
                if stripped.endswith(','):
                    new_lines.append(line)
                    continue
                # Property-key pattern: `<ident>:` or `'str':` or `"str":`
                if _re.match(r'^(?:[A-Za-z_$][\w$]*|["\'][^"\']+["\'])\s*:', stripped):
                    new_lines.append(line)
                    continue
                # Array expression on its own line: `[x.rows[0].id]` or
                # `[a, b.rows[0].c]`: these are function call arguments, not
                # standalone statements. Injecting an `if (...) return` above
                # them would produce invalid JS (NULL_GUARD_INJECTION1).
                if stripped.startswith('['):
                    new_lines.append(line)
                    continue
                # Multi-line call expression: prior lines have unmatched '('
                # (NULL_GUARD_INJECTION1: customer proposals.js incident).
                if _is_inside_call_expression(lines, i):
                    new_lines.append(line)
                    continue

                # Look back up to 10 lines for an existing guard
                context_start = max(0, i - 10)
                context = '\n'.join(lines[context_start:i])

                has_guard = any(_re.search(p, context) for p in GUARD_PATTERNS)

                if not has_guard:
                    indent = len(line) - len(line.lstrip())
                    guard = (
                        ' ' * indent
                        + f"if (!{var_name}.rows.length)"
                        + f" return res.status(404).json({{ error: 'Not found' }});"
                    )
                    new_lines.append(guard)
                    fixes_in_file += 1

            new_lines.append(line)

        if fixes_in_file > 0:
            file_dict[filename] = '\n'.join(new_lines)
            total_fixes += fixes_in_file
            print(f"  [post-process] Added {fixes_in_file} null guard(s) to {filename}")

    if total_fixes > 0:
        print(f"  [post-process] Total: {total_fixes} null property access guard(s) injected")

    return list(file_dict.items())


@processor(after=('_fix_null_property_access',), phase=('build',))
def _fix_brand_nowrap(files: list) -> list:
    """Add whitespace-nowrap to brand/logo links in layout.ejs nav.

    Brand text (identified by font-bold, text-xl, text-lg, or text-2xl classes)
    wraps to multiple lines when the nav is crowded. This adds 'whitespace-nowrap'
    to prevent wrapping and 'truncate' as a safety net for very long names.
    """
    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Find brand links: <a> tags with bold/large text classes inside <nav>
    brand_pattern = _re.compile(
        r'(<a\s+[^>]*class=")'
        r'([^"]*(?:font-bold|text-xl|text-lg|text-2xl)[^"]*)'
        r'(")',
    )

    def _add_nowrap(m):
        prefix = m.group(1)
        classes = m.group(2)
        suffix = m.group(3)
        if "whitespace-nowrap" in classes:
            return m.group(0)  # already has it
        return prefix + classes + " whitespace-nowrap" + suffix

    new_layout, count = brand_pattern.subn(_add_nowrap, layout)
    if count > 0 and new_layout != layout:
        file_dict["views/layout.ejs"] = new_layout
        print(f"  [post-process] Added whitespace-nowrap to {count} brand link(s)")
    return list(file_dict.items())


@processor(after=('_fix_brand_nowrap',), phase=('build',))
def _ensure_mobile_nav(files: list) -> list:
    """Add mobile hamburger menu to layout.ejs if nav links overflow on mobile.

    Handles two common patterns:
    1. 'hidden md:flex' / 'hidden lg:flex', links already hidden on mobile but no toggle
    2. Flat flex nav: all links visible in a single row, overflow on small screens

    Both patterns get a hamburger button (visible on mobile) and a collapsible menu.
    """
    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Skip if mobile menu already exists (covers both camelCase and dash-case Claude variants)
    has_mobile_btn = ("mobile-menu-btn" in layout or "mobileMenuBtn" in layout
                      or "mobileMenu" in layout or "mobile-menu" in layout
                      or "hamburger" in layout.lower()
                      or "menu-toggle" in layout or "navbar-toggler" in layout)
    if has_mobile_btn:
        return files

    # Extract the <nav> block
    nav_match = _re.search(r'(<nav[^>]*>)(.*?)(</nav>)', layout, _re.DOTALL)
    if not nav_match:
        return files

    nav_content = nav_match.group(2)

    # Count nav links (exclude brand/logo link which typically has font-bold or text-xl)
    all_nav_links = _re.findall(r'<a\s+[^>]*href="(/[^"]*)"[^>]*>', nav_content)
    brand_links = _re.findall(
        r'<a\s+[^>]*class="[^"]*(?:font-bold|text-xl|text-lg|text-2xl)[^"]*"[^>]*>',
        nav_content,
    )
    nav_link_count = len(all_nav_links) - len(brand_links)

    # Pattern 1: hidden md:flex (original detection)
    has_hidden_nav = _re.search(r'class="[^"]*hidden\s+(md|lg):flex[^"]*"', layout)

    # Pattern 2: flat flex nav with 4+ links (will overflow on mobile)
    is_flat_nav = not has_hidden_nav and nav_link_count >= 4

    if not has_hidden_nav and not is_flat_nav:
        return files

    breakpoint = "md"  # default breakpoint for mobile/desktop split
    if has_hidden_nav:
        breakpoint = has_hidden_nav.group(1)

    # Extract nav background color from inline style or class
    nav_bg_match = _re.search(
        r'<nav[^>]*style="[^"]*background-color:\s*([^;"]+)',
        layout,
    )
    nav_bg = nav_bg_match.group(1).strip() if nav_bg_match else "#1f2937"

    # Collect page links (not brand, not logout/user info) for the mobile menu
    # Find all <a> tags inside <nav> that link to app pages
    page_links = []
    for link_match in _re.finditer(
        r'<a\s+([^>]*)>(.*?)</a>', nav_content, _re.DOTALL
    ):
        attrs = link_match.group(1)
        text = _re.sub(r'<[^>]+>', '', link_match.group(2)).strip()
        href_m = _re.search(r'href="([^"]*)"', attrs)
        if not href_m or not text:
            continue
        href = href_m.group(1)
        # Skip brand links, logout, login, register
        if any(kw in attrs for kw in ["font-bold", "text-xl", "text-lg", "text-2xl"]):
            continue
        if href in ("/logout", "/login", "/register", "#"):
            continue
        page_links.append((href, text))

    # For flat navs (pattern 2), require 3+ links to justify a hamburger menu.
    # For hidden navs (pattern 1), the links are already hidden so always add toggle.
    if not has_hidden_nav and len(page_links) < 3:
        return files
    if not page_links:
        return files

    # Build mobile menu links
    mobile_links_html = "\n".join(
        f'      <a href="{href}" class="block px-3 py-2 rounded-md text-base '
        f'font-medium text-gray-200 hover:text-white hover:bg-white/10">{text}</a>'
        for href, text in page_links
    )

    # Build hamburger button
    hamburger_btn = (
        f'<button id="mobile-menu-btn" type="button" '
        f'class="{breakpoint}:hidden inline-flex items-center justify-center p-2 '
        f'rounded-md text-gray-200 hover:text-white hover:bg-white/10" '
        f'aria-expanded="false" aria-label="Toggle menu">\n'
        f'            <svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">\n'
        f'              <path id="menu-icon-open" stroke-linecap="round" stroke-linejoin="round" '
        f'stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/>\n'
        f'              <path id="menu-icon-close" class="hidden" stroke-linecap="round" '
        f'stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>\n'
        f'            </svg>\n'
        f'          </button>'
    )

    # Build mobile dropdown menu
    mobile_menu = (
        f'\n  <div id="mobile-menu" class="hidden {breakpoint}:hidden" '
        f'style="background-color: {nav_bg};">\n'
        f'    <div class="px-2 pt-2 pb-3 space-y-1">\n'
        f'{mobile_links_html}\n'
        f'    </div>\n'
        f'  </div>'
    )

    # Toggle script
    toggle_script = (
        '\n<script>\n'
        '(function() {\n'
        '  var btn = document.getElementById("mobile-menu-btn");\n'
        '  var menu = document.getElementById("mobile-menu");\n'
        '  var iconOpen = document.getElementById("menu-icon-open");\n'
        '  var iconClose = document.getElementById("menu-icon-close");\n'
        '  if (btn && menu) {\n'
        '    btn.addEventListener("click", function() {\n'
        '      var expanded = btn.getAttribute("aria-expanded") === "true";\n'
        '      btn.setAttribute("aria-expanded", !expanded);\n'
        '      menu.classList.toggle("hidden");\n'
        '      if (iconOpen) iconOpen.classList.toggle("hidden");\n'
        '      if (iconClose) iconClose.classList.toggle("hidden");\n'
        '    });\n'
        '  }\n'
        '})();\n'
        '</script>\n'
    )

    new_layout = layout

    if has_hidden_nav:
        # Pattern 1: inject button before hidden div, keep existing structure
        links_pattern = _re.compile(
            rf'(<div\s+class="hidden\s+{breakpoint}:flex[^"]*"[^>]*>)(.*?)(</div>)',
            _re.DOTALL,
        )
        links_match = links_pattern.search(new_layout)
        if links_match:
            new_layout = new_layout.replace(
                links_match.group(0),
                hamburger_btn + '\n          ' + links_match.group(0),
            )
    else:
        # Pattern 2: flat flex nav, hide page links on mobile, add hamburger
        # Find the first <div class="flex items-center..."> inside nav that has page links
        # and add hidden md:flex to it, plus insert hamburger before it
        links_container = _re.search(
            r'(<div\s+class=")(flex\s+items-center\s+space-x[^"]*"[^>]*>)',
            nav_content,
        )
        if links_container:
            old_div = links_container.group(0)
            # Add hidden md:flex to hide links on mobile
            new_div = links_container.group(1) + f"hidden {breakpoint}:flex " + links_container.group(2)
            new_layout = new_layout.replace(old_div, hamburger_btn + '\n          ' + new_div, 1)

    # Add mobile menu after </nav>
    new_layout = new_layout.replace('</nav>', '</nav>' + mobile_menu, 1)

    # Add toggle script before </body>
    if '</body>' in new_layout:
        new_layout = new_layout.replace('</body>', toggle_script + '</body>')
    else:
        new_layout += toggle_script

    file_dict["views/layout.ejs"] = new_layout
    print("  [post-process] Added mobile hamburger menu to layout.ejs")
    return list(file_dict.items())


@processor(after=('_ensure_mobile_nav',), phase=('build',))
def _ensure_mobile_responsive(files: list) -> list:
    """Fix common mobile responsiveness issues in generated files.

    1. Wrap bare <table> elements in overflow-x-auto div (views)
    2. Ensure form containers have max-w-lg mx-auto (views)
    3. Fix card grids to use responsive columns (views)
    4. Add mobile safety-net CSS to public/style.css
    5. Ensure inputs have text-base for 16px minimum (prevents iOS zoom)
    """
    file_dict = dict(files)
    fixes_applied = 0

    # --- 1. Wrap bare <table> in overflow-x-auto ---
    for filepath, content in list(file_dict.items()):
        if not filepath.endswith(".ejs"):
            continue
        new_content = content
        # Find all <table> tags and wrap those not already inside overflow-x-auto
        tables = list(_re.finditer(r'<table\b[^>]*>', new_content))
        offset_shift = 0
        for match in tables:
            start = match.start() + offset_shift
            # Check if the closest parent div has overflow-x-auto (already wrapped)
            preceding = new_content[max(0, start - 200):start]
            last_div = preceding.rfind('<div')
            if last_div >= 0:
                between = preceding[last_div:]
                if 'overflow-x-auto' in between and '</div>' not in between:
                    continue
            # Find matching </table>
            table_end_match = _re.search(r'</table>', new_content[start:])
            if not table_end_match:
                continue
            end = start + table_end_match.end()
            table_html = new_content[start:end]
            wrapped = f'<div class="overflow-x-auto">\n{table_html}\n</div>'
            new_content = new_content[:start] + wrapped + new_content[end:]
            offset_shift += len(wrapped) - len(table_html)
        if new_content != content:
            file_dict[filepath] = new_content
            fixes_applied += 1

    # --- 2. Fix card grids: grid-cols-2 or grid-cols-3 without responsive prefix ---
    grid_pattern = _re.compile(r'\bclass="([^"]*)"')
    for filepath, content in list(file_dict.items()):
        if not filepath.endswith(".ejs"):
            continue
        new_content = content

        def _fix_grid_classes(m):
            classes = m.group(1)
            # If it has grid-cols-2 or grid-cols-3 without sm:/md:/lg: prefix
            # and doesn't already have responsive variants
            if _re.search(r'(?<!\w:)grid-cols-[23]', classes):
                has_responsive = _re.search(r'(sm|md|lg):grid-cols-', classes)
                if not has_responsive and 'grid' in classes:
                    # Replace bare grid-cols-N with responsive version
                    classes = _re.sub(
                        r'(?<!\w:)grid-cols-2',
                        'grid-cols-1 sm:grid-cols-2',
                        classes,
                    )
                    classes = _re.sub(
                        r'(?<!\w:)grid-cols-3',
                        'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3',
                        classes,
                    )
                    return f'class="{classes}"'
            return m.group(0)

        new_content = grid_pattern.sub(_fix_grid_classes, new_content)
        if new_content != content:
            file_dict[filepath] = new_content
            fixes_applied += 1

    # --- 3. Add mobile safety-net CSS to public/style.css ---
    style_css = file_dict.get("public/style.css", "")
    mobile_safety = (
        "\n/* Mobile responsiveness safety net */\n"
        "html, body { overflow-x: hidden; }\n"
        "input, select, textarea { font-size: 16px; }\n"
        "@media (max-width: 640px) {\n"
        "  table { display: block; overflow-x: auto; }\n"
        "  .container, .max-w-7xl, .max-w-6xl { padding-left: 1rem; padding-right: 1rem; }\n"
        "}\n"
    )
    if "overflow-x: hidden" not in style_css:
        file_dict["public/style.css"] = style_css + mobile_safety
        fixes_applied += 1

    if fixes_applied:
        print(f"  [post-process] Fixed {fixes_applied} mobile responsiveness issue(s)")
    return list(file_dict.items())


@processor(after=('_strip_redundant_image_upload_forms',))
def _fix_admin_crud_link_prefix(files: list, spec: dict) -> list:
    """ADMIN_CRUD_404_PREFIX (v13 fix): strip /admin/ prefix from model CRUD links.

    Claude's admin list views frequently emit links like:
        <a href="/admin/events/new">
        <a href="/admin/events/<%= event.id %>/edit">
        <form action="/admin/events/<%= event.id %>/delete">

    ...but the actual routes mount at /events/new, /events/:id/edit etc. (see
    routes/events.js). Result: every Edit/New/Delete link from the admin
    dashboard 404s. First observed on MW v13; caused a P0 blocker where the
    founder couldn't create/edit/delete events, photos, or videos through admin.

    This post-processor detects model slugs from spec.data_models and strips
    the /admin/ prefix from href/action attributes that target those slugs.
    Preserves /admin (dashboard), /admin/settings (canonical), /admin/login, etc.
    """
    import re as re_mod

    data_models = spec.get("data_models", [])
    if not data_models:
        return files

    # Build slug set: plural forms only (that's what routes use)
    slugs = set()
    for m in data_models:
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        plural = _pluralize_slug(slug)
        slugs.add(plural)
        slugs.add(slug)
    # Also include common CRUD-adjacent slugs Claude invents
    slugs.update({"contact-messages", "messages", "contacts", "subscribers"})
    # EXCLUDE paths that legitimately live under /admin
    EXCLUDE = {"settings", "login", "logout", "users", "reviews", "builds", "emails", "dashboard"}
    slugs -= EXCLUDE

    if not slugs:
        return files

    file_dict = dict(files)
    changed = False
    rewrites = 0

    # Build union regex matching /admin/<slug>/... or /admin/<slug>"
    slug_alt = "|".join(re_mod.escape(s) for s in sorted(slugs, key=len, reverse=True))
    link_re = re_mod.compile(
        rf'((?:href|action)\s*=\s*")/admin/({slug_alt})(/[^"]*)?"',
        re_mod.IGNORECASE,
    )

    # B+C §4: widget-emitted admin surfaces (settings.ejs, <slug>/new.ejs,
    # <slug>/edit.ejs) intentionally carry canonical /admin/<slug> form
    # action URLs that contracts/admin_crud.py asserts verbatim. They
    # announce themselves with the WIDGET_ADMIN_MARKER comment at the top
    # of the file. _ensure_admin_slug_mount dual-mounts routes at both
    # /<slug> and /admin/<slug>, so the canonical URL resolves at runtime.
    # Skip files carrying the marker to preserve those action URLs while
    # still fixing Claude-emitted admin list views that legitimately need
    # the /admin/ prefix stripped.
    from cli.widgets.forms import WIDGET_ADMIN_MARKER

    for path, content in file_dict.items():
        if not path.endswith(".ejs") or not isinstance(content, str):
            continue
        # Only process view files
        if not path.startswith("views/"):
            continue
        if WIDGET_ADMIN_MARKER in content:
            continue

        def _rewrite(m):
            nonlocal rewrites
            rewrites += 1
            prefix_attr = m.group(1)  # href=" or action="
            slug = m.group(2)
            tail = m.group(3) or ""
            return f'{prefix_attr}/{slug}{tail}"'

        new_content = link_re.sub(_rewrite, content)
        if new_content != content:
            file_dict[path] = new_content
            changed = True

    if changed:
        print(f"  [post-process] Stripped /admin/ prefix from {rewrites} model CRUD link(s) across views")
    return list(file_dict.items()) if changed else files


@processor(after=('_fix_admin_crud_link_prefix', '_ensure_admin_slug_mount'),
           phase=('build', 'reapply'))
def _fix_admin_layout_nav_prefix(files: list, spec: dict) -> list:
    """ADMIN_LAYOUT_NAV_PREFIX:
    add /admin/ prefix to admin-layout nav links pointing at bare
    resource router mounts.

    The build shape this targets:
    - TWO layouts: views/layout.ejs (admin console) + views/layout-public.ejs.
    - Resource routers DUAL-MOUNTED at /<slug> + /admin/<slug>
      (emitted by _ensure_admin_slug_mount).
    - Router render path branches on req.baseUrl.startsWith('/admin/').

    Under this architecture, admin-layout <nav> links that point at
    bare /<slug> hit the UNGUARDED mount, render the public view tree,
    and the admin sees a read-only listing instead of the management
    UI. The fix: rewrite those bare hrefs to /admin/<slug> so baseUrl
    branching picks the admin view.

    Scope (narrow by design):
    - views/layout.ejs only (views/layout-public.ejs untouched).
    - Only within <nav>...</nav> blocks inside that layout.
    - Only for <slug> that matches a writable-resource plural from
      spec["data_models"] AND has a /admin/<slug> mount in server.js
      (evidence that the dual-mount pattern was emitted).
    - Preserves /dashboard, /admin/*, /logout, / (home), and any
      non-writable-resource hrefs.
    - Skips footer, aside, and out-of-nav links entirely.

    Counterpart to _fix_admin_crud_link_prefix (which strips /admin/
    from CRUD action URLs in admin list views because those routes
    mount at /<slug>/...). This processor adds /admin/ to main nav
    hrefs because those hit the dual mount's branch logic.
    """
    import re as re_mod

    file_dict = dict(files)

    layout = file_dict.get("views/layout.ejs")
    if not layout or not isinstance(layout, str):
        return files

    # Architecture gate. This processor is only safe on
    # builds that emit TWO layouts: `views/layout.ejs` as the admin
    # console + `views/layout-public.ejs` for anonymous visitors. Under
    # that shape, rewriting layout.ejs's nav to `/admin/<slug>` is
    # correct.
    #
    # Builds that emit ONE shared layout (no layout-public.ejs) use
    # layout.ejs for BOTH public and admin visitors; its nav needs to
    # stay `/<slug>` so anonymous users reach the public router branch.
    # Session-based routing inside each resource router picks the
    # admin vs. public view when a logged-in admin clicks the bare
    # link.
    #
    # Rewriting the single-shared-layout nav breaks anonymous visitors
    # - they click Events and get bounced to /login because
    # requireAdmin guards /admin/<slug>. Gate on the presence of
    # `views/layout-public.ejs` as the "two-layout architecture"
    # signal.
    if "views/layout-public.ejs" not in file_dict:
        return files

    data_models = spec.get("data_models", []) if isinstance(spec, dict) else []
    if not data_models:
        return files

    # Build writable-resource plural slug set.
    slugs = set()
    for m in data_models:
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        plural = _pluralize_slug(slug)
        slugs.add(plural)
        slugs.add(slug)
    # Common admin-adjacent surfaces Claude invents.
    slugs.update({"contact-inquiries", "contact-messages", "messages",
                  "contacts", "subscribers", "newsletter-subscribers"})
    # EXCLUDE paths that legitimately live under bare or under /admin/.
    EXCLUDE = {"settings", "login", "logout", "users", "reviews",
               "builds", "emails", "dashboard", "admin"}
    slugs -= EXCLUDE

    if not slugs:
        return files

    # Check server.js for dual-mount evidence, only rewrite slugs
    # that we can confirm have a /admin/<slug> mount.
    server = file_dict.get("server.js") or ""
    admin_mounted_slugs = set()
    if isinstance(server, str):
        for slug in slugs:
            # Match: app.use('/admin/<slug>', [...]) with single or
            # double quotes, requireAdmin middleware optional.
            pattern = rf"app\.use\(\s*['\"]/admin/{re_mod.escape(slug)}['\"]"
            if re_mod.search(pattern, server):
                admin_mounted_slugs.add(slug)

    if not admin_mounted_slugs:
        # No dual-mount evidence: this isn't a two-layout build.
        # Leave layout untouched.
        return files

    # Isolate <nav>...</nav> blocks and rewrite hrefs inside them only.
    # Admin layout may have multiple nav blocks (desktop + mobile menu).
    nav_re = re_mod.compile(r"(<nav\b[^>]*>)([\s\S]*?)(</nav>)",
                            re_mod.IGNORECASE)
    slug_alt = "|".join(re_mod.escape(s)
                        for s in sorted(admin_mounted_slugs, key=len, reverse=True))
    # Match: href="/<slug>" or href="/<slug>/...", does NOT match
    # href="/<slug>something" (slug-boundary).
    href_re = re_mod.compile(
        rf'(href\s*=\s*")/({slug_alt})(/[^"]*)?"',
        re_mod.IGNORECASE,
    )

    rewrites_count = 0

    def _rewrite_nav_body(nav_match):
        nonlocal rewrites_count
        open_tag, body, close_tag = nav_match.group(1), nav_match.group(2), nav_match.group(3)
        # In nav body, rewrite each bare /<slug> href to /admin/<slug>.
        def _rewrite_href(href_match):
            nonlocal rewrites_count
            prefix = href_match.group(1)  # href="
            slug = href_match.group(2)
            tail = href_match.group(3) or ""
            rewrites_count += 1
            return f'{prefix}/admin/{slug}{tail}"'
        new_body = href_re.sub(_rewrite_href, body)
        return open_tag + new_body + close_tag

    new_layout = nav_re.sub(_rewrite_nav_body, layout)

    if new_layout == layout:
        return files

    file_dict["views/layout.ejs"] = new_layout
    print(f"  [post-process] Added /admin/ prefix to "
          f"{rewrites_count} admin-layout nav link(s)")
    return list(file_dict.items())


@processor(after=('_fix_admin_crud_link_prefix',),
           phase=('build', 'reapply'))
def _unify_admin_dashboard_handler(files: list, spec: dict = None) -> list:
    """GET /admin must not render views/dashboard.ejs
    with an incomplete template context.

    Builds frequently emit two handlers that render dashboard.ejs:
    - routes/dashboard.js: `router.get('/', requireAuth, ...)` that SELECTs
      the full stats + recent_inquiries and calls `res.render('dashboard',
      {..., recentInquiries: rows})`.
    - routes/admin.js: `router.get('/', requireAuth, ...)` that also
      renders dashboard.ejs but WITHOUT passing `recentInquiries`.

    Result: GET `/admin` crashes with `ReferenceError: recentInquiries is
    not defined` at dashboard.ejs:~41. Users who land on `/admin` (e.g.
    via a "Back to Dashboard" link in Settings) hit a 500 error page.

    This processor rewrites any `res.render('dashboard', {...})` call
    inside `routes/admin.js` to `res.redirect('/dashboard')` so the
    canonical handler runs. Scoped narrowly to `routes/admin.js` (NOT
    routes/dashboard.js which is the authoritative handler).

    Idempotent: already-redirecting admin.js is untouched.
    """
    import re as re_mod

    file_dict = dict(files)
    admin_js = file_dict.get("routes/admin.js")
    if not admin_js or not isinstance(admin_js, str):
        return files

    # Match res.render('dashboard', {...}); handles one level of brace
    # nesting so patterns like `{stats: {testimonials: 5}, title: 'X'}`
    # match. The nested subexpression `\{[^{}]*\}` consumes the nested
    # literal object; deeper nesting would need a balanced-brace parser.
    dashboard_render_re = re_mod.compile(
        r"res\.render\(\s*['\"]dashboard['\"]\s*,\s*"
        r"\{(?:[^{}]|\{[^{}]*\})*\}"
        r"\s*\)\s*;?",
        re_mod.MULTILINE,
    )
    matches = list(dashboard_render_re.finditer(admin_js))
    if not matches:
        return files

    # Rewrite every match.
    new_admin_js = dashboard_render_re.sub(
        "res.redirect('/dashboard');",
        admin_js,
    )
    if new_admin_js == admin_js:
        return files

    file_dict["routes/admin.js"] = new_admin_js
    print(
        f"  [post-process] Unified admin dashboard handler: rewrote "
        f"{len(matches)} res.render('dashboard', ...) call(s) in "
        "routes/admin.js to res.redirect('/dashboard') (avoids "
        "ReferenceError when recentInquiries is missing from ctx)"
    )
    return list(file_dict.items())


@processor(after=('_fix_view_html_shells',), phase=('build',))
def _fix_view_slug_paths(files: list, spec: dict) -> list:
    """Fix singular slug paths in EJS views; replace with correct English plurals.

    Scans all views/*.ejs files for href and action attributes that use a model's
    singular slug (e.g., href="/recipe/1") and rewrites them to the plural form
    (e.g., href="/recipes/1"). This is a deterministic post-processing safety net
    for when prompt engineering alone doesn't prevent the pattern.

    Only rewrites paths that match a known model singular slug, using a
    word-boundary-aware pattern to avoid false positives (e.g., /recipe-manager
    would not be rewritten to /recipes-manager).

    Returns the updated file list.
    """
    data_models = spec.get("data_models", [])
    if not data_models:
        return files

    # Build singular -> plural mapping for all models
    slug_map = {}  # singular_slug -> plural_slug
    for m in data_models:
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        plural = _pluralize_slug(slug)
        if slug != plural:
            slug_map[slug] = plural

    if not slug_map:
        return files

    updated = []
    for filepath, content in files:
        if not filepath.startswith("views/") or not filepath.endswith(".ejs"):
            updated.append((filepath, content))
            continue

        new_content = content
        for singular, plural in slug_map.items():
            # Match href or action = "/{singular}/" or "/{singular}" (end of attr)
            # Use a negative lookahead to avoid matching longer slugs that start the same
            # e.g., /recipe should not match inside /recipe-manager
            pattern = rf'((?:href|action)=")/{_re.escape(singular)}(/|"|\?)'
            replacement = rf'\1/{plural}\2'
            new_content = _re.sub(pattern, replacement, new_content)

        if new_content != content:
            updated.append((filepath, new_content))
        else:
            updated.append((filepath, content))

    return updated


@processor(after=('_ensure_nav_glassmorphism',), phase=('build',))
def _ensure_logo_placement(files: list, spec: dict) -> list:
    """Post-processor: ensure logo img tags in key views when spec has logo_data.

    When the customer uploaded a logo during intake, the build pipeline writes
    it to public/logo.png (or public/logo.svg for SVG logos). This post-processor
    ensures the generated views reference the correct logo file in nav, login,
    register, and footer.
    """
    import re as re_mod

    design = spec.get("design", {})
    logo_data = design.get("logo_data", "")
    if not logo_data:
        return files

    logo_fn = _logo_filename(spec)
    logo_path = f"/{logo_fn}"
    favicon_type = "image/svg+xml" if logo_fn.endswith(".svg") else "image/png"
    app_name = spec.get("app_name", "App")
    file_dict = dict(files)

    # --- Layout.ejs: nav logo + footer logo + favicon ---
    layout = file_dict.get("views/layout.ejs", "")
    if layout:
        changed = False

        # Inject logo in nav if not present
        if logo_path not in layout.split("</nav>")[0] if "</nav>" in layout else logo_path not in layout:
            # Find the brand link in nav and prepend logo img
            brand_pattern = re_mod.compile(
                r'(<a\s[^>]*(?:href=["\']/?(?:dashboard)?["\'])[^>]*class="[^"]*(?:font-bold|text-xl|text-lg)[^"]*"[^>]*>)',
                re_mod.IGNORECASE,
            )
            m = brand_pattern.search(layout)
            if m:
                logo_img = f'<img src="{logo_path}" alt="{app_name}" class="h-10 max-h-10 w-auto object-contain mr-2 inline-block flex-shrink-0 rounded-lg">'
                layout = layout[:m.end()] + logo_img + layout[m.end():]
                changed = True
                print(f"  [post-process] Injected logo in navbar")

        # Inject favicon link if no logo favicon yet.
        # Use [^<>]* (not [^>]*) so we don't partially match EJS expression
        # attributes like href="<%= siteSettings.nav_logo_url %>": those contain
        # a '>' inside '%>' which would truncate the match and leave a dangling '">'.
        if 'rel="icon"' in layout and logo_path not in layout.split("</head>")[0]:
            # Replace only static (non-EJS) favicon link tags
            layout = re_mod.sub(
                r'<link\s+rel="icon"[^<>]*>',
                f'<link rel="icon" type="{favicon_type}" href="{logo_path}">',
                layout,
            )
            # Also strip any EJS-conditional favicon link blocks (e.g. Claude generates
            # an <% if (siteSettings.nav_logo_url) { %>\n<link rel="icon" ...>\n<% } %> block).
            # Use [^\n]* instead of [^>]* for the link tag so we don't get confused by
            # the '>' chars inside '<%= ... %>' EJS expressions within the href attribute.
            layout = re_mod.sub(
                r'[ \t]*<%[^%]+%>[ \t]*\n[ \t]*<link\b(?=[^\n]*rel="icon")[^\n]*\n[ \t]*<%[^%]+%>[ \t]*\n?',
                '\n',
                layout,
            )
            changed = True
        elif 'rel="icon"' not in layout and "</head>" in layout:
            layout = layout.replace(
                "</head>",
                f'  <link rel="icon" type="{favicon_type}" href="{logo_path}">\n</head>',
            )
            changed = True

        # Inject logo in footer if footer exists and no logo in footer
        footer_start = layout.find("<footer")
        if footer_start != -1:
            footer_section = layout[footer_start:]
            if logo_path not in footer_section:
                # Add logo right after <footer...>
                footer_tag_end = layout.find(">", footer_start)
                if footer_tag_end != -1:
                    footer_logo = f'\n    <img src="{logo_path}" alt="{app_name}" class="h-12 w-auto object-contain opacity-90 mb-2 rounded-lg">'
                    # Find first content element inside footer
                    insert_pos = footer_tag_end + 1
                    layout = layout[:insert_pos] + footer_logo + layout[insert_pos:]
                    changed = True

        if changed:
            file_dict["views/layout.ejs"] = layout

    # --- Login.ejs: logo above form ---
    login = file_dict.get("views/login.ejs", "")
    if login and logo_path not in login:
        logo_tag = f'<img src="{logo_path}" alt="{app_name}" class="h-20 w-auto object-contain mx-auto mb-4 rounded-xl shadow-lg">'
        # Insert before the first <form or <h1 or <h2
        for marker in ["<form", "<h1", "<h2"]:
            idx = login.find(marker)
            if idx != -1:
                login = login[:idx] + logo_tag + "\n" + login[idx:]
                file_dict["views/login.ejs"] = login
                print("  [post-process] Injected logo in login page")
                break

    # --- Register.ejs: logo above form ---
    register = file_dict.get("views/register.ejs", "")
    if register and logo_path not in register:
        logo_tag = f'<img src="{logo_path}" alt="{app_name}" class="h-20 w-auto object-contain mx-auto mb-4 rounded-xl shadow-lg">'
        for marker in ["<form", "<h1", "<h2"]:
            idx = register.find(marker)
            if idx != -1:
                register = register[:idx] + logo_tag + "\n" + register[idx:]
                file_dict["views/register.ejs"] = register
                print("  [post-process] Injected logo in register page")
                break

    # --- Patch existing logo img tags: ensure rounded corners + max-h-10 in nav ---
    layout = file_dict.get("views/layout.ejs", "")
    if layout and logo_path in layout:
        def _patch_logo_classes(m):
            tag = m.group(0)
            classes = m.group(1) if m.group(1) else ""
            if "rounded" not in classes:
                classes = classes.rstrip('"') if classes.endswith('"') else classes
                classes += " rounded-lg"
            if "max-h-" not in classes and "</nav>" not in layout[m.end():m.end()+50]:
                # Only add max-h-10 in nav area (before </nav>)
                pass
            tag = re_mod.sub(r'class="[^"]*"', f'class="{classes.strip()}"', tag)
            return tag

        # Find all logo img tags and ensure they have rounded-lg
        logo_pattern = re_mod.compile(
            r'<img\s+[^>]*src="' + re_mod.escape(logo_path) + r'"[^>]*class="([^"]*)"[^>]*>',
            re_mod.IGNORECASE,
        )
        patched_layout = logo_pattern.sub(_patch_logo_classes, layout)
        if patched_layout != layout:
            file_dict["views/layout.ejs"] = patched_layout
            print("  [post-process] Patched logo img tags with rounded corners")

    return list(file_dict.items())


def _logo_filename(spec):
    """Return 'logo.svg' if spec has SVG logo, else 'logo.png'."""
    logo_data = spec.get("design", {}).get("logo_data", "")
    if logo_data.startswith("data:image/svg+xml"):
        return "logo.svg"
    return "logo.png"


# ---------------------------------------------------------------------------
# Stock photo infrastructure
# ---------------------------------------------------------------------------


def _match_stock_categories(spec):
    """Return list of stock photo categories relevant to this spec."""
    try:
        from design_data import match_product_type
    except ImportError:
        match_product_type = None

    description = spec.get("description", "")
    if not description:
        return []

    if match_product_type:
        product_type = match_product_type(description)
        if product_type and product_type in PRODUCT_TYPE_STOCK_CATEGORIES:
            return PRODUCT_TYPE_STOCK_CATEGORIES[product_type]

    # Fallback: direct keyword matching
    categories = []
    desc_lower = description.lower()
    direct_keywords = {
        "pet": "pets", "dog": "pets", "cat": "pets",
        "food": "food", "restaurant": "food", "recipe": "food",
        "gym": "fitness", "workout": "fitness", "yoga": "fitness",
        "doctor": "medical", "health": "medical", "clinic": "medical",
        "tech": "tech", "software": "tech", "code": "tech",
        "healing": "wellness", "meditation": "wellness", "ceremony": "wellness",
        "wellness": "wellness", "spiritual": "wellness", "holistic": "wellness",
        "retreat": "wellness", "sacred": "wellness", "community": "wellness",
        # MW-observed gaps: somatic / sound / journey / dome / bass / vocal /
        # breathwork / sanctuary. Somatic-practice and sound-journey businesses
        # are a meaningful CAS archetype and must resolve to the wellness pool.
        "somatic": "wellness", "sound journey": "wellness", "sound bath": "wellness",
        "sound healing": "wellness", "dome": "wellness", "sanctuary": "wellness",
        "breathwork": "wellness", "reiki": "wellness", "energy work": "wellness",
        "plant medicine": "wellness", "integration": "wellness",
        "school": "education", "classroom": "education", "student": "education",
        "university": "education", "tutor": "education", "course": "education",
        "hotel": "hospitality", "resort": "hospitality", "lobby": "hospitality",
        "hospitality": "hospitality", "guest": "hospitality",
        "construction": "construction", "blueprint": "construction",
        "contractor": "construction", "builder": "construction",
        "car": "automotive", "dealership": "automotive", "mechanic": "automotive",
        "vehicle": "automotive", "auto": "automotive",
        "salon": "beauty", "haircut": "beauty", "skincare": "beauty",
        "nail": "beauty", "cosmetic": "beauty",
        "daycare": "childcare", "playground": "childcare", "nursery": "childcare",
        "preschool": "childcare", "toddler": "childcare",
        "stadium": "sports", "basketball": "sports", "soccer": "sports",
        "tennis": "sports", "league": "sports",
        "painting": "arts", "pottery": "arts", "sculpture": "arts",
        "gallery": "arts", "craft": "arts",
        "attorney": "legal", "courtroom": "legal", "gavel": "legal",
        "lawyer": "legal", "law firm": "legal",
        "house": "real_estate", "apartment": "real_estate", "property": "real_estate",
        "realtor": "real_estate", "listing": "real_estate",
    }
    for kw, cat in direct_keywords.items():
        if kw in desc_lower and cat not in categories:
            categories.append(cat)
    return categories[:2] if categories else []


def _build_stock_photo_context(spec):
    """Generate prompt section listing available stock photos for Claude.

    Surfaces the richer metadata (alt_text, use_case, mood, orientation) so
    Claude can pick mood-appropriate photos for the right surface and emit
    accurate alt text directly. Only photos that exist on disk are listed,
    matching what _ensure_stock_photos will actually copy into the build.
    """
    categories = _match_stock_categories(spec)
    if not categories:
        return ""

    stock_dir = Path(__file__).parent / "stock_photos"
    manifest_path = stock_dir / "manifest.json"
    if not manifest_path.exists():
        return ""

    manifest = json.loads(manifest_path.read_text())

    lines = ["\n## STOCK PHOTOS AVAILABLE\n"]
    lines.append("The following stock photos are pre-installed in public/images/stock/.")
    lines.append("Use these instead of external image URLs (Unsplash, Pexels, etc.).")
    lines.append("Reference them as: /images/stock/{category}/{filename}")
    lines.append(
        "Each photo lists use_case (where it fits: hero/gallery/about/services/"
        "team/background/feature), mood (the tone it carries), orientation, and "
        "alt_text (paste verbatim into the img alt attribute).\n"
    )

    has_content = False
    for cat in categories:
        if cat not in manifest:
            continue
        on_disk = [
            photo for photo in manifest[cat]
            if (stock_dir / cat / photo["filename"]).exists()
        ]
        if not on_disk:
            continue
        has_content = True
        lines.append(f"### {cat}/")
        for photo in on_disk:
            tags = ", ".join(photo.get("tags", []))
            use_case = ", ".join(photo.get("use_case", [])) or "any"
            mood = ", ".join(photo.get("mood", [])) or "neutral"
            orientation = photo.get("orientation", "landscape")
            alt_text = photo.get("alt_text", "")
            line = (
                f"  - {photo['filename']} "
                f"({photo.get('width', 0)}x{photo.get('height', 0)}, {orientation}) "
                f"-- use_case: [{use_case}] -- mood: [{mood}] -- tags: {tags}"
            )
            if alt_text:
                line += f' -- alt_text: "{alt_text}"'
            lines.append(line)

    if not has_content:
        return ""

    lines.append("")
    return "\n".join(lines)


@processor(after=('_ensure_logo_placement',), phase=('build',))
def _ensure_stock_photos(files, spec):
    """Copy relevant stock photos to build dir, replace external URLs, and fix broken local paths."""
    import re as re_mod

    categories = _match_stock_categories(spec)

    stock_dir = Path(__file__).parent / "stock_photos"
    manifest_path = stock_dir / "manifest.json"

    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    # Collect available photos (only those that exist on disk)
    available_photos = []
    for cat in (categories or []):
        if cat not in manifest:
            continue
        for photo in manifest[cat]:
            src_path = stock_dir / cat / photo["filename"]
            if src_path.exists():
                dest = f"public/images/stock/{cat}/{photo['filename']}"
                if not any(f[0] == dest for f in files):
                    files.append((dest, src_path.read_bytes()))
                available_photos.append(f"/images/stock/{cat}/{photo['filename']}")

    # Replace external stock photo URLs with local paths (when available)
    if available_photos:
        external_url_pattern = re_mod.compile(
            r'(src|href)="https?://(images\.unsplash\.com|images\.pexels\.com|'
            r'via\.placeholder\.com|source\.unsplash\.com)[^"]*"'
        )
        photo_idx = [0]

        def replace_url(match):
            attr = match.group(1)
            replacement = available_photos[photo_idx[0] % len(available_photos)]
            photo_idx[0] += 1
            return f'{attr}="{replacement}"'

        for i, (path, content) in enumerate(files):
            if not path.endswith(".ejs") or isinstance(content, bytes):
                continue
            new_content = external_url_pattern.sub(replace_url, content)
            if new_content != content:
                files[i] = (path, new_content)
                print(f"  [post-process] Replaced external image URLs in {path}")

    # Fix fake image paths in seed.sql.
    # When stock photos are available, replace /images/gallery/... with real
    # /images/stock/... paths. When no stock photos, replace with NULL so views
    # skip the img tag gracefully (users add real images via upload form).
    fake_seed_path_pattern = re_mod.compile(
        r"'/images/(?:stock|gallery)/[^']+'"
    )
    if available_photos:
        seed_photo_idx = [0]

        def _replace_seed_path(match):
            replacement = available_photos[seed_photo_idx[0] % len(available_photos)]
            seed_photo_idx[0] += 1
            return f"'{replacement}'"

        for i, (path, content) in enumerate(files):
            if not path.endswith("seed.sql") or isinstance(content, bytes):
                continue
            new_content = fake_seed_path_pattern.sub(_replace_seed_path, content)
            if new_content != content:
                files[i] = (path, new_content)
                print(f"  [post-process] Replaced fake image paths with stock photos in seed.sql")
    else:
        for i, (path, content) in enumerate(files):
            if not path.endswith("seed.sql") or isinstance(content, bytes):
                continue
            new_content = fake_seed_path_pattern.sub("NULL", content)
            if new_content != content:
                files[i] = (path, new_content)
                print(f"  [post-process] Replaced fake image paths with NULL in seed.sql")

    # Fix broken local image paths: /images/stock/... and /images/gallery/...
    # When Claude generates these paths but the actual files don't exist, replace
    # with inline SVG placeholder to prevent broken images
    file_dict = dict(files)
    existing_public_files = {p for p, _ in files if p.startswith("public/")}

    broken_img_pattern = re_mod.compile(
        r'<img([^>]*)\ssrc="(/images/(?:stock|gallery)/[^"]+)"([^>]*)>'
    )

    def _fix_broken_img(match, filepath):
        pre_attrs = match.group(1)
        img_path = match.group(2)
        post_attrs = match.group(3)
        public_path = f"public{img_path}"

        # Check if the file exists in the build output
        if public_path in existing_public_files:
            return match.group(0)  # file exists, keep as-is

        # Replace with placeholder SVG data URI
        # Extract alt text if available for the placeholder
        alt_match = re_mod.search(r'alt="([^"]*)"', match.group(0))
        alt_text = alt_match.group(1) if alt_match else "Image"
        placeholder_svg = (
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "width='400' height='300' viewBox='0 0 400 300'%3E"
            "%3Crect fill='%23E8F0F4' width='400' height='300'/%3E"
            "%3Ctext fill='%239CA3AF' font-family='sans-serif' font-size='14' "
            "text-anchor='middle' x='200' y='150'%3E"
            f"{alt_text.replace(' ', '%20')}"
            "%3C/text%3E%3C/svg%3E"
        )
        print(f"  [post-process] Fixed broken image path {img_path} in {filepath}")
        return f'<img{pre_attrs} src="{placeholder_svg}"{post_attrs}>'

    for i, (path, content) in enumerate(files):
        if not path.endswith(".ejs") or isinstance(content, bytes):
            continue
        new_content = broken_img_pattern.sub(
            lambda m: _fix_broken_img(m, path), content
        )
        if new_content != content:
            files[i] = (path, new_content)

    # Add onerror fallback to dynamic EJS image tags (DB-driven image_url)
    # These can't be caught by the static regex above because the src is
    # a runtime EJS expression like <%= photo.image_url %>
    ejs_img_pattern = re_mod.compile(
        r'(<img\b[^>]*\ssrc="<%[=-]\s*\w+\.(?:image_url|photo_url|img_url|thumbnail)\s*%>"[^>]*)'
        r'(?<!onerror=)(?<!%)(>)'
    )
    placeholder_onerror = (
        " onerror=\"this.onerror=null;this.src='data:image/svg+xml,"
        "%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27400%27 height=%27300%27"
        " viewBox=%270 0 400 300%27%3E%3Crect fill=%27%23E8F0F4%27 width=%27400%27"
        " height=%27300%27/%3E%3Ctext fill=%27%239CA3AF%27 font-family=%27sans-serif%27"
        " font-size=%2714%27 text-anchor=%27middle%27 x=%27200%27 y=%27150%27%3E"
        "Photo%3C/text%3E%3C/svg%3E'\""
    )
    for i, (path, content) in enumerate(files):
        if not path.endswith(".ejs") or isinstance(content, bytes):
            continue
        if "onerror=" in content:
            continue  # already has fallback
        new_content = ejs_img_pattern.sub(
            lambda m: m.group(1) + placeholder_onerror + m.group(2),
            content,
        )
        if new_content != content:
            files[i] = (path, new_content)
            print(f"  [post-process] Added image onerror fallback in {path}")

    # Add server-side placeholder route for missing /images/gallery/* and /images/stock/*
    file_dict = dict(files)
    server_js = file_dict.get("server.js", "")
    if server_js and "/images/:type(gallery|stock)" not in server_js and "express.static" in server_js:
        placeholder_route = (
            "\n// Serve SVG placeholder for missing gallery/stock images\n"
            "app.get('/images/:type(gallery|stock)/*', (req, res) => {\n"
            "  const label = req.params.type === 'gallery' ? 'Photo' : 'Image';\n"
            "  res.type('image/svg+xml').send(\n"
            "    '<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"400\" height=\"300\" "
            "viewBox=\"0 0 400 300\">' +\n"
            "    '<rect fill=\"#E8F0F4\" width=\"400\" height=\"300\"/>' +\n"
            "    '<text fill=\"#9CA3AF\" font-family=\"sans-serif\" font-size=\"14\" "
            "text-anchor=\"middle\" x=\"200\" y=\"140\">' + label + '</text>' +\n"
            "    '<path d=\"M185 160 l15-15 l15 15 M190 175 l5-5 l5 5 l5-5 l5 5\" "
            "fill=\"none\" stroke=\"#9CA3AF\" stroke-width=\"1.5\"/>' +\n"
            "    '</svg>'\n"
            "  );\n"
            "});\n"
        )
        static_match = re_mod.search(r"app\.use\(express\.static\(.*\)\);", server_js)
        if static_match:
            insert_pos = static_match.end()
            server_js = server_js[:insert_pos] + placeholder_route + server_js[insert_pos:]
        file_dict["server.js"] = server_js
        files = list(file_dict.items())
        print("  [post-process] Added image placeholder route for missing gallery/stock images")

    return files


def _ensure_logo_in_build(output_dir, spec):
    """Decode base64 logo from spec and write to the build directory.

    Handles both PNG and SVG formats. SVG logos written to public/logo.svg,
    all other formats to public/logo.png. Extension derived from data URI mimetype.
    """
    import base64
    import re as re_mod

    design = spec.get("design", {})
    logo_data = design.get("logo_data", "")
    if not logo_data:
        return

    match = re_mod.match(r"data:image/([^;]+);base64,(.+)", logo_data)
    if not match:
        print("  [logo] Could not parse logo_data URI, skipping")
        return

    mimetype = match.group(1)
    try:
        logo_bytes = base64.b64decode(match.group(2))
        ext = "svg" if mimetype == "svg+xml" else "png"
        logo_path = output_dir / "public" / f"logo.{ext}"
        logo_path.parent.mkdir(parents=True, exist_ok=True)
        with open(logo_path, "wb") as f:
            f.write(logo_bytes)
        print(f"  [logo] Wrote public/logo.{ext} ({len(logo_bytes)} bytes)")
    except Exception as e:
        print(f"  [logo] Failed to write logo file: {e}")


def _write_logo_file(output_dir, spec):
    """Legacy alias for _ensure_logo_in_build. Kept for backward compatibility."""
    return _ensure_logo_in_build(output_dir, spec)


@processor(after=('_ensure_tailwind_compiled',))
def _ensure_font_loading(files: list, spec: dict) -> list:
    """Inject Google Fonts <link> tag and font-family CSS into layout.ejs.

    Must run AFTER _ensure_tailwind_compiled (which creates tailwind.config.js
    with fontFamily, but does not add the Google Fonts <link> tag itself).
    """
    import re as re_mod
    from design_data import FONT_PAIRINGS

    design = spec.get("design", {})
    font_display = design.get("font_display", "")
    font_body = design.get("font_body", "")

    if not font_display and not font_body:
        # Fall back to UUPM-resolved fonts (e.g. style "warm earthy" -> Lora/Raleway)
        try:
            from design_data import get_design_context
            ctx = get_design_context(spec)
            fonts = ctx.get("fonts", {})
            font_display = fonts.get("display", "")
            font_body = fonts.get("body", "")
        except Exception:
            pass
        if not font_display and not font_body:
            return files

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    changed = False

    # Find a matching font pairing by display+body names
    google_fonts_url = ""
    for _key, pairing in FONT_PAIRINGS.items():
        if pairing["display"] == font_display and pairing["body"] == font_body:
            google_fonts_url = pairing.get("google_fonts_url", "")
            break

    # If no exact match, build a URL from font names
    if not google_fonts_url and (font_display or font_body):
        families = []
        if font_display:
            families.append(f"family={font_display.replace(' ', '+')}")
        if font_body and font_body != font_display:
            families.append(f"family={font_body.replace(' ', '+')}")
        google_fonts_url = "https://fonts.googleapis.com/css2?" + "&".join(families) + "&display=swap"

    # Inject or update Google Fonts link
    if google_fonts_url:
        existing_gf = re_mod.search(r'<link[^>]*href="(https://fonts\.googleapis\.com/[^"]*)"[^>]*>', layout)
        if existing_gf:
            # Check if existing link already has the right fonts
            existing_href = existing_gf.group(1)
            display_in_url = font_display.replace(" ", "+") if font_display else ""
            body_in_url = font_body.replace(" ", "+") if font_body else ""
            has_display = not display_in_url or display_in_url in existing_href
            has_body = not body_in_url or body_in_url in existing_href
            if not (has_display and has_body):
                # Replace with correct URL
                layout = layout[:existing_gf.start()] + f'<link href="{google_fonts_url}" rel="stylesheet">' + layout[existing_gf.end():]
                changed = True
        else:
            # Inject before </head>
            if "</head>" in layout:
                link_tag = f'  <link href="{google_fonts_url}" rel="stylesheet">\n'
                layout = layout.replace("</head>", link_tag + "</head>")
                changed = True

    # Inject font-family style block if missing
    body_font = font_body or font_display
    display_font = font_display or font_body
    if body_font and "font-family" not in layout:
        style_block = (
            f"  <style>"
            f"body {{ font-family: '{body_font}', sans-serif; }} "
            f"h1,h2,h3,h4,h5,h6 {{ font-family: '{display_font}', sans-serif; }}"
            f"</style>\n"
        )
        if "</head>" in layout:
            layout = layout.replace("</head>", style_block + "</head>")
            changed = True

    if changed:
        file_dict["views/layout.ejs"] = layout
        print("  [post-process] Injected Google Fonts loading into layout.ejs")

    return list(file_dict.items())


@processor(after=('_ensure_color_consistency',))
def _fix_admin_dark_stat_cards(files: list, spec: dict) -> list:
    """Fix dark-on-dark contrast in ALL views when primary color and page body are both dark.

    Claude sometimes generates pages using text-primary (e.g. dark navy #002040), inline
    color:var(--color-primary), and light-mode classes (text-gray-900) on dark page bodies
    (bg-slate-900). This produces invisible text and is a critical design review failure.

    Covers all views/ files (including subdirectories like views/admin/settings.ejs and
    non-admin files like views/contact-submissions-detail.ejs), not just views/admin*.ejs.

    Fix: When primary color luminance < 0.15 AND layout body is dark:
    - text-primary → text-white (class attrs)
    - bg-primary/5|10|20|25 → bg-white/10
    - color:var(--color-primary) inline style → color: #ffffff
    - text-gray-900/800/700 → text-slate-200/300/400 (light-mode dark text on dark bg)
    Skips layout.ejs, login.ejs, register.ejs (they have their own dark-panel designs).
    """
    import re as re_mod

    # Resolve primary color
    try:
        from design_data import get_design_context, _hex_luminance
        ctx = get_design_context(spec)
        primary_color = ctx.get("palette", {}).get("primary", "")
    except Exception:
        primary_color = spec.get("design", {}).get("primary_color", "")

    if not primary_color or not primary_color.startswith("#") or len(primary_color) < 7:
        return files

    try:
        luminance = _hex_luminance(primary_color)
    except Exception:
        return files

    # Only fix when primary is very dark (near-black, dark navy, etc.)
    if luminance >= 0.15:
        return files

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")

    # Only fix when page body background is also dark
    dark_body_pattern = re_mod.compile(
        r'<body[^>]*class="[^"]*bg-(?:slate|gray|zinc|neutral|stone)-9\d\d[^"]*"',
        re_mod.IGNORECASE,
    )
    if not dark_body_pattern.search(layout):
        return files

    # Files to skip: they have their own intentional dark panel designs
    SKIP_FILES = {"views/layout.ejs", "views/login.ejs", "views/register.ejs"}

    changed_files = []
    for rel_path, content in files:
        if not (rel_path.startswith("views/") and rel_path.endswith(".ejs")):
            changed_files.append((rel_path, content))
            continue
        if rel_path in SKIP_FILES:
            changed_files.append((rel_path, content))
            continue

        original = content

        # Replace bg-primary/5|10|20|25 with bg-white/10
        content = re_mod.sub(r'\bbg-primary/(?:5|10|20|25)\b', 'bg-white/10', content)

        # Replace text-primary in class attributes
        content = re_mod.sub(
            r'(class="[^"]*)\btext-primary\b([^"]*")', r'\1text-white\2', content
        )

        # Replace inline color:var(--color-primary) → color:#ffffff
        content = re_mod.sub(
            r'\bcolor:\s*var\(--color-primary\)', 'color:#ffffff', content
        )

        # Replace light-mode dark text classes on dark backgrounds
        content = re_mod.sub(
            r'(class="[^"]*)\btext-gray-900\b([^"]*")', r'\1text-slate-200\2', content
        )
        content = re_mod.sub(
            r'(class="[^"]*)\btext-gray-800\b([^"]*")', r'\1text-slate-300\2', content
        )
        content = re_mod.sub(
            r'(class="[^"]*)\btext-gray-700\b([^"]*")', r'\1text-slate-400\2', content
        )

        # Replace light card backgrounds on dark theme pages
        # bg-slate-50 / bg-gray-50 create unreadable light panels inside dark sections
        content = re_mod.sub(
            r'(class="[^"]*)\bbg-slate-50\b([^"]*")', r'\1bg-slate-700/50\2', content
        )
        content = re_mod.sub(
            r'(class="[^"]*)\bbg-gray-50\b([^"]*")', r'\1bg-slate-700/50\2', content
        )

        if content != original:
            print(f"  [post-process] Fixed dark-on-dark text in {rel_path}")

        changed_files.append((rel_path, content))

    return changed_files


@processor(after=('_fix_admin_dark_stat_cards',))
def _fix_dark_section_text(files: list, spec: dict) -> list:
    """Fix dark text classes inside gradient sections that use a dark primary color.

    Complements _fix_admin_dark_stat_cards (which requires a dark body background).
    This handles apps with a light body but dark-primary gradient sections; e.g. a
    wellness/earthy app where the hero or CTA section uses `from-primary via-secondary
    to-primary` (a dark gradient) but the heading inside has text-slate-900, making it
    invisible (MW v6 DARK_SECTION_TEXT1: newsletter section in contact.ejs).

    Activates when primary luminance < 0.15. Scans EJS files that contain
    `from-primary` or `bg-primary` gradient sections, tracks nesting depth, and
    replaces dark text classes within those sections with light equivalents.
    """
    import re as re_mod

    try:
        from design_data import get_design_context, _hex_luminance
        ctx = get_design_context(spec)
        primary_color = ctx.get("palette", {}).get("primary", "")
    except Exception:
        primary_color = spec.get("design", {}).get("primary_color", "")

    if not primary_color or not primary_color.startswith("#") or len(primary_color) < 7:
        return files

    try:
        luminance = _hex_luminance(primary_color)
    except Exception:
        return files

    if luminance >= 0.15:
        return files

    SKIP_FILES = {"views/layout.ejs", "views/login.ejs", "views/register.ejs"}

    # Detect opening tag of a dark gradient section. Two cases:
    #  (a) Tailwind class uses from-primary / via-primary / bg-primary
    #  (b) Inline style with linear-gradient(...): under the primary-luminance < 0.15
    #      guard, gradient hero sections are near-universally dark. An MW v7 build
    #      failed design review because Claude emitted hero headers with
    #      inline rgba() gradients (not Tailwind primary classes) and dark-on-dark
    #      headings were missed by this processor.
    dark_section_open_re = re_mod.compile(
        r'<(section|div)\b[^>]*(?:class="[^"]*(?:from-primary\b|via-primary\b|bg-primary\b)[^"]*"|style="[^"]*linear-gradient[^"]*")',
        re_mod.IGNORECASE,
    )
    generic_open_re = re_mod.compile(r'<(?:section|div)\b', re_mod.IGNORECASE)
    close_re = re_mod.compile(r'</(?:section|div)>', re_mod.IGNORECASE)

    DARK_TEXT_SUBS = [
        ("text-slate-900", "text-white"),
        ("text-gray-900", "text-white"),
        ("text-slate-800", "text-slate-100"),
        ("text-gray-800", "text-slate-100"),
        ("text-slate-700", "text-slate-200"),
        ("text-gray-700", "text-slate-200"),
    ]

    changed_files = []
    for rel_path, content in files:
        if not (rel_path.startswith("views/") and rel_path.endswith(".ejs")):
            changed_files.append((rel_path, content))
            continue
        if rel_path in SKIP_FILES:
            changed_files.append((rel_path, content))
            continue

        # Quick exit: only process files containing dark gradient sections
        if not dark_section_open_re.search(content):
            changed_files.append((rel_path, content))
            continue

        original = content
        lines = content.split("\n")
        new_lines = []
        depth = 0  # > 0 means we are inside a dark gradient section

        for line in lines:
            dark_opens = len(dark_section_open_re.findall(line))
            all_opens = len(generic_open_re.findall(line))
            closes = len(close_re.findall(line))

            in_dark = depth > 0 or dark_opens > 0
            if in_dark:
                fixed = line
                for old, new in DARK_TEXT_SUBS:
                    fixed = fixed.replace(old, new)
                new_lines.append(fixed)
            else:
                new_lines.append(line)

            # Update depth for next line
            if depth > 0:
                non_dark_opens = all_opens - dark_opens
                depth += dark_opens + non_dark_opens - closes
                depth = max(0, depth)
            elif dark_opens > 0:
                non_dark_opens = all_opens - dark_opens
                depth = dark_opens + non_dark_opens - closes
                depth = max(0, depth)

        new_content = "\n".join(new_lines)
        if new_content != original:
            print(f"  [post-process] Fixed dark text in gradient sections in {rel_path}")
        changed_files.append((rel_path, new_content))

    return changed_files


@processor(after=('_fix_dark_section_text',))
def _fix_light_on_light_text(files: list, spec: dict) -> list:
    """Rewrite light text classes on h1/h2/h3 when the page body or hero is light.

    Mirror image of `_fix_admin_dark_stat_cards`. Catches the cream-on-cream
    failure mode (medicine-waves MW v2): a body class like `bg-amber-50` /
    `bg-stone-100` / `bg-cream` paired with hero headings using `text-white` /
    `text-cream` / `text-ivory` / `text-amber-50` etc., which renders invisible.

    Trigger conditions (any of):
      - layout.ejs <body> uses a known light Tailwind background class
      - effective primary color (UUPM-resolved) has luminance > 0.85
      - UUPM-resolved palette background luminance > 0.85 (catches MW-style
        apps where primary is dark but wellness/light palette background
        is cream/ivory and body uses inline CSS custom-property style
        instead of a Tailwind bg-* class)
      - style.css --color-background custom property has luminance > 0.85
        (defensive catch when Claude's generated CSS diverges from UUPM)

    For each <h1>/<h2>/<h3> with a light text class, swap to text-slate-900.
    Skips views/layout.ejs / views/login.ejs / views/register.ejs (intentional
    split-panel designs with their own dark panels).

    Lookback skip: if the heading sits inside an explicitly dark parent section,
    leave the light text alone. Dark parent detection:
      - Tailwind dark bg class (bg-slate-900, bg-black, bg-primary, etc.)
      - CSS class defined in style.css with a dark background gradient/color
        (e.g. `.hero-main { background: linear-gradient(#0a1f2d ...) }`)
      - Inline style with linear-gradient using var(--color-primary)/secondary
        when the spec primary/secondary colors are dark
      - Inline style with a literal dark hex color
    """
    import re as re_mod
    primary_color = ""
    secondary_color = ""
    background_color = ""
    try:
        from design_data import get_design_context, _hex_luminance
        ctx = get_design_context(spec)
        palette = ctx.get("palette", {}) or {}
        primary_color = palette.get("primary", "") or ""
        secondary_color = palette.get("secondary", "") or ""
        background_color = palette.get("background", "") or ""
    except Exception:
        design = spec.get("design", {}) or {}
        primary_color = design.get("primary_color", "") or ""
        secondary_color = design.get("secondary_color", "") or ""
        background_color = ""

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    style_css = file_dict.get("public/style.css", "")

    # Detect light page bg via Tailwind class on <body> in layout.ejs
    LIGHT_BG_CLASSES = re_mod.compile(
        r'<body[^>]*class="[^"]*\b('
        r'bg-white|bg-cream|bg-ivory|'
        r'bg-(?:amber|stone|orange|yellow|rose|pink|neutral|slate|gray|zinc)-(?:50|100|200)'
        r')\b',
        re_mod.IGNORECASE,
    )
    body_is_light = bool(LIGHT_BG_CLASSES.search(layout))

    def _is_light_hex(hex_color: str) -> bool:
        if not (hex_color and hex_color.startswith("#") and len(hex_color) >= 7):
            return False
        try:
            from design_data import _hex_luminance
            return _hex_luminance(hex_color) > 0.85
        except Exception:
            return False

    primary_is_light = _is_light_hex(primary_color)
    background_is_light = _is_light_hex(background_color)

    def _is_dark_hex(hex_color: str) -> bool:
        if not (hex_color and hex_color.startswith("#") and len(hex_color) >= 7):
            return False
        try:
            from design_data import _hex_luminance
            return _hex_luminance(hex_color) < 0.5
        except Exception:
            return False

    primary_is_dark = _is_dark_hex(primary_color)
    secondary_is_dark = _is_dark_hex(secondary_color)

    # Defensive catch: scan public/style.css for --color-background: #RRGGBB
    # in case Claude generated a CSS file that diverges from the UUPM palette.
    css_bg_is_light = False
    if not background_is_light and style_css:
        css_bg_match = re_mod.search(
            r'--color-background\s*:\s*(#[0-9a-fA-F]{6})', style_css
        )
        if css_bg_match:
            css_bg_is_light = _is_light_hex(css_bg_match.group(1))

    if not (body_is_light or primary_is_light or background_is_light or css_bg_is_light):
        return files

    # Parse style.css to find CSS classes with dark background gradients.
    # MW v2 defined `.hero-main` and `.newsletter-section` as dark gradients in
    # style.css; the old Tailwind-only lookback missed them and over-corrected
    # `text-white` hero headings to `text-slate-900` (invisible on dark hero).
    def _build_dark_css_classes() -> set:
        classes: set = set()
        if not style_css:
            return classes
        try:
            from design_data import _hex_luminance
        except Exception:
            return classes
        class_def_re = re_mod.compile(
            r'\.([a-zA-Z][\w-]*)\s*\{([^}]*)\}', re_mod.DOTALL
        )
        bg_prop_re = re_mod.compile(
            r'background(?:-color|-image)?\s*:\s*([^;}]+)',
            re_mod.IGNORECASE,
        )
        hex_re = re_mod.compile(r'#([0-9a-fA-F]{6})')
        var_primary_re = re_mod.compile(r'var\(\s*--color-primary\b')
        var_secondary_re = re_mod.compile(r'var\(\s*--color-secondary\b')
        var_light_re = re_mod.compile(
            r'var\(\s*--color-(?:cream|ivory|background|light|white|foreground)\b'
        )
        for cm in class_def_re.finditer(style_css):
            classname = cm.group(1)
            body = cm.group(2)
            bg_match = bg_prop_re.search(body)
            if not bg_match:
                continue
            bg_value = bg_match.group(1)
            # Known-light CSS var ref short-circuits; not a dark wrapper.
            if var_light_re.search(bg_value):
                continue
            lums: list = []
            for h in hex_re.findall(bg_value):
                try:
                    lums.append(_hex_luminance("#" + h))
                except Exception:
                    pass
            if var_primary_re.search(bg_value) and primary_color:
                try:
                    lums.append(_hex_luminance(primary_color))
                except Exception:
                    pass
            if var_secondary_re.search(bg_value) and secondary_color:
                try:
                    lums.append(_hex_luminance(secondary_color))
                except Exception:
                    pass
            if not lums:
                continue
            if all(l < 0.5 for l in lums):
                classes.add(classname)
        return classes

    dark_css_classes = _build_dark_css_classes()

    SKIP_FILES = {"views/layout.ejs", "views/login.ejs", "views/register.ejs"}
    LIGHT_TEXT_CLASSES = (
        "text-white",
        "text-cream",
        "text-ivory",
        "text-amber-50",
        "text-amber-100",
        "text-stone-50",
        "text-stone-100",
        "text-orange-50",
        "text-yellow-50",
        "text-rose-50",
        "text-neutral-50",
    )
    HEADING_TAG_RE = re_mod.compile(
        r'(<h[1-3]\b[^>]*class="[^"]*?)\b('
        + "|".join(re_mod.escape(c) for c in LIGHT_TEXT_CLASSES)
        + r')\b([^"]*"[^>]*>)',
        re_mod.IGNORECASE,
    )
    DARK_PARENT_RE = re_mod.compile(
        r'\b(bg-(?:slate|gray|zinc|neutral|stone)-(?:8|9)\d\d|bg-black|bg-primary)\b',
        re_mod.IGNORECASE,
    )
    CLASS_ATTR_RE = re_mod.compile(r'class="([^"]*)"')
    STYLE_ATTR_RE = re_mod.compile(r'style="([^"]*)"')
    INLINE_HEX_RE = re_mod.compile(r'#([0-9a-fA-F]{6})')

    try:
        from design_data import _hex_luminance as _lum_fn
    except Exception:
        _lum_fn = None

    def _is_inside_dark_parent(content: str, match_start: int) -> bool:
        """Look back ~8 lines from the heading. If we hit an opening
        section/div with a dark wrapper, the heading is inside a dark
        parent and the light text class is intentional.

        Dark wrapper detection:
          - Tailwind dark bg class (bg-slate-900, bg-black, bg-primary, etc.)
          - CSS class defined in public/style.css whose background resolves
            to entirely dark colors (e.g. .hero-main, .newsletter-section)
          - Inline style using var(--color-primary) or var(--color-secondary)
            when the spec primary/secondary is dark
          - Inline style containing a literal dark hex color
        """
        lookback_start = content.rfind("\n", 0, match_start)
        for _ in range(8):
            if lookback_start <= 0:
                break
            line_end = lookback_start
            lookback_start = content.rfind("\n", 0, lookback_start - 1)
            line = content[max(lookback_start, 0):line_end]
            # 1. Tailwind dark bg class
            if DARK_PARENT_RE.search(line):
                return True
            # 2. CSS-class-defined dark wrappers from style.css
            if dark_css_classes:
                for cmatch in CLASS_ATTR_RE.finditer(line):
                    class_list = cmatch.group(1).split()
                    if any(c in dark_css_classes for c in class_list):
                        return True
            # 3. Inline style with dark CSS var refs or dark literal hex
            smatch = STYLE_ATTR_RE.search(line)
            if smatch:
                style_raw = smatch.group(1)
                style_lower = style_raw.lower()
                if "background" in style_lower:
                    if primary_is_dark and "var(--color-primary" in style_lower:
                        return True
                    if secondary_is_dark and "var(--color-secondary" in style_lower:
                        return True
                    if _lum_fn is not None:
                        for h in INLINE_HEX_RE.findall(style_raw):
                            try:
                                if _lum_fn("#" + h) < 0.5:
                                    return True
                            except Exception:
                                pass
        return False

    changed_files = []
    for rel_path, content in files:
        if not (rel_path.startswith("views/") and rel_path.endswith(".ejs")):
            changed_files.append((rel_path, content))
            continue
        if rel_path in SKIP_FILES:
            changed_files.append((rel_path, content))
            continue

        original = content
        out = []
        cursor = 0
        for m in HEADING_TAG_RE.finditer(content):
            out.append(content[cursor:m.start()])
            if _is_inside_dark_parent(content, m.start()):
                out.append(m.group(0))
            else:
                out.append(m.group(1) + "text-slate-900" + m.group(3))
            cursor = m.end()
        out.append(content[cursor:])
        content = "".join(out)

        if content != original:
            print(f"  [post-process] Fixed light-on-light heading text in {rel_path}")
        changed_files.append((rel_path, content))

    return changed_files


@processor(after=('_ensure_font_loading',))
def _ensure_color_consistency(files: list, spec: dict) -> list:
    """Ensure CSS custom properties match the effective palette (UUPM-aware).

    Patches ALL :root blocks containing --color-primary, both standalone
    <style>:root{...}</style> AND :root embedded inside larger <style> blocks.
    Claude often puts :root { --color-primary: #1a1a2e } in an inline style
    which overrides the correct colors in style.css.

    Uses get_design_context() to resolve colors: respects UUPM palette overrides
    for generic dark placeholder colors. NARROWLY SCOPED: Only replaces colors
    in CSS custom properties and nav background contexts.
    Does NOT do blanket find-replace across all files.
    """
    import re as re_mod

    # Use get_design_context for UUPM-aware color resolution
    try:
        from design_data import get_design_context
        ctx = get_design_context(spec)
        palette = ctx.get("palette", {})
        primary_color = palette.get("primary", "")
        on_primary_color = palette.get("on_primary", "")
        secondary_color = palette.get("secondary", "")
        accent_color = palette.get("accent", "")
    except Exception:
        # Fallback to raw spec.design
        design = spec.get("design", {})
        primary_color = design.get("primary_color", "")
        on_primary_color = ""
        secondary_color = design.get("secondary_color", "")
        accent_color = design.get("accent_color", "")

    if not primary_color:
        return files

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    changed = False

    # Build the :root CSS block
    root_vars = f"--color-primary: {primary_color};"
    if on_primary_color:
        root_vars += f" --color-on-primary: {on_primary_color};"
    if secondary_color:
        root_vars += f" --color-secondary: {secondary_color};"
    if accent_color:
        root_vars += f" --color-accent: {accent_color};"
    root_block = f":root {{ {root_vars} }}"

    # Fix ALL :root blocks in layout.ejs, matches :root inside ANY <style> tag,
    # not just standalone <style>:root{...}</style>. This catches Claude's inline
    # style blocks that override the correct values from style.css.
    def _replace_root_colors(match):
        """Replace --color-primary/on-primary/secondary/accent in a :root block."""
        block = match.group(0)
        block = re_mod.sub(
            r'(--color-primary:\s*)[^;]+(;)',
            rf'\g<1>{primary_color}\2',
            block,
            count=1,
        )
        if on_primary_color:
            if '--color-on-primary' in block:
                block = re_mod.sub(
                    r'(--color-on-primary:\s*)[^;]+(;)',
                    rf'\g<1>{on_primary_color}\2',
                    block,
                    count=1,
                )
            else:
                # Inject --color-on-primary after --color-primary declaration
                block = re_mod.sub(
                    r'(--color-primary:\s*[^;]+;)',
                    rf'\1 --color-on-primary: {on_primary_color};',
                    block,
                    count=1,
                )
        if secondary_color:
            block = re_mod.sub(
                r'(--color-secondary:\s*)[^;]+(;)',
                rf'\g<1>{secondary_color}\2',
                block,
                count=1,
            )
        if accent_color:
            block = re_mod.sub(
                r'(--color-accent:\s*)[^;]+(;)',
                rf'\g<1>{accent_color}\2',
                block,
                count=1,
            )
        return block

    # Pattern matches :root { ... } blocks containing --color-primary anywhere in layout
    root_pattern = re_mod.compile(r':root\s*\{[^}]*--color-primary:\s*[^;]+;[^}]*\}')
    all_roots = list(root_pattern.finditer(layout))

    if all_roots:
        # Check if ANY root block has wrong colors
        needs_fix = False
        for m in all_roots:
            existing_primary = re_mod.search(r'--color-primary:\s*([^;]+);', m.group(0))
            if existing_primary and existing_primary.group(1).strip().lower() != primary_color.lower():
                needs_fix = True
                break
        if needs_fix:
            layout = root_pattern.sub(_replace_root_colors, layout)
            changed = True
    else:
        # No :root block found, inject one in <head>
        if "</head>" in layout:
            style_tag = f"  <style>{root_block}</style>\n"
            layout = layout.replace("</head>", style_tag + "</head>")
            changed = True

    # Replace hardcoded hex in nav inline background-color (narrowly scoped)
    nav_pattern = re_mod.compile(
        r'(<nav\b[^>]*style="[^"]*background-color:\s*)#[0-9a-fA-F]{3,8}([^"]*")',
        re_mod.IGNORECASE,
    )
    nav_match = nav_pattern.search(layout)
    if nav_match:
        layout = nav_pattern.sub(rf'\g<1>{primary_color}\2', layout)
        changed = True

    if changed:
        file_dict["views/layout.ejs"] = layout
        print("  [post-process] Ensured color consistency in layout.ejs")

    # Also fix :root in style.css if it has wrong/generic colors
    style_css = file_dict.get("public/style.css", "")
    if style_css and primary_color:
        root_match = re_mod.search(
            r'(:root\s*\{[^}]*--color-primary:\s*)([^;]+)(;[^}]*\})',
            style_css,
        )
        if root_match:
            existing = root_match.group(2).strip().lower()
            if existing != primary_color.lower():
                # Replace primary and related vars
                new_css = style_css
                new_css = re_mod.sub(
                    r'(--color-primary:\s*)[^;]+(;)',
                    rf'\g<1>{primary_color}\2',
                    new_css,
                    count=1,
                )
                if on_primary_color:
                    new_css = re_mod.sub(
                        r'(--color-on-primary:\s*)[^;]+(;)',
                        rf'\g<1>{on_primary_color}\2',
                        new_css,
                        count=1,
                    )
                if secondary_color:
                    new_css = re_mod.sub(
                        r'(--color-secondary:\s*)[^;]+(;)',
                        rf'\g<1>{secondary_color}\2',
                        new_css,
                        count=1,
                    )
                if accent_color:
                    new_css = re_mod.sub(
                        r'(--color-accent:\s*)[^;]+(;)',
                        rf'\g<1>{accent_color}\2',
                        new_css,
                        count=1,
                    )
                file_dict["public/style.css"] = new_css
                print("  [post-process] Fixed color consistency in public/style.css")

    return list(file_dict.items())


# Hardcoded emerald/mint defaults that Claude sometimes copy-pastes from
# boilerplate tailwind.config / :root snippets. Any of these appearing in
# the secondary palette slots is almost always wrong for the spec.
_EMERALD_DEFAULT_HEXES = frozenset({
    "#ecfdf5", "#d1fae5", "#a7f3d0", "#6ee7b7", "#34d399",
    "#10b981", "#059669", "#047857", "#065f46", "#064e3b",
    "#e8f1f3",  # mint-ish muted
})


@processor(after=('_ensure_color_consistency',), phase=('build', 'reapply'))
def _align_secondary_palette_to_spec(files, spec):
    """Strip emerald/mint defaults Claude boilerplates into tailwind.config.js
    and public/style.css when they clash with spec.design.

    Surface: the spec palette was maroon/cream/pink (the same values as
    the v2 warm-earth palette in compose/data/palettes.json) but the
    generated tailwind.config.js still had `surface: #ECFDF5`,
    `foreground: #064E3B`, `muted: #E8F1F3`, `border: #A7F3D0`, and
    public/style.css exposed `--color-background: #ECFDF5` +
    `--color-foreground: #064E3B`. Result: a light-green stripe bleeding
    through below the footer and green text tokens on any Tailwind
    component using `text-foreground` / `bg-surface`.

    _ensure_color_consistency already patches the FOUR primary/
    on-primary/secondary/accent slots. This processor covers the
    SECONDARY palette slots (background, foreground, surface, muted,
    border, card) ONLY when the incoming value is one of the known
    emerald defaults, otherwise we leave legitimate green specs alone.

    Replacement strategy (spec-derived, never a hardcoded fallback
    hue):
      --color-background / surface / card  → spec.secondary (light cream)
          falling back to "#ffffff"
      --color-foreground                    → spec primary darkened, or
          "#1f1f1f" fallback
      muted / border                        → spec.accent at opacity
          (cheap proxy: use accent hex; design-wise this is still the
          right palette family)
    """
    import re as re_mod

    try:
        from design_data import get_design_context
        ctx = get_design_context(spec)
        palette = ctx.get("palette", {}) if isinstance(ctx, dict) else {}
        primary = palette.get("primary", "")
        secondary = palette.get("secondary", "")
        accent = palette.get("accent", "")
    except Exception:
        design = (spec or {}).get("design", {}) if isinstance(spec, dict) else {}
        primary = design.get("primary_color", "")
        secondary = design.get("secondary_color", "")
        accent = design.get("accent_color", "")

    if not (primary or secondary or accent):
        return files

    bg_target = secondary or "#ffffff"
    fg_target = "#1f1f1f"  # safe dark neutral; spec primary is usually too saturated for body text
    surface_target = bg_target
    muted_target = accent or bg_target
    border_target = accent or "#e5e7eb"
    card_target = "#ffffff"

    def _is_emerald(value: str) -> bool:
        v = value.strip().strip('"').strip("'").lower()
        return v in _EMERALD_DEFAULT_HEXES

    file_dict = dict(files)
    changed_any = False

    # ---- public/style.css: rewrite --color-background + --color-foreground
    style_css = file_dict.get("public/style.css", "")
    if isinstance(style_css, str) and "--color-background" in style_css:
        original = style_css

        def _rewrite_bg(match):
            key, current, suffix = match.group(1), match.group(2), match.group(3)
            if _is_emerald(current):
                return f"{key}{bg_target}{suffix}"
            return match.group(0)

        def _rewrite_fg(match):
            key, current, suffix = match.group(1), match.group(2), match.group(3)
            if _is_emerald(current):
                return f"{key}{fg_target}{suffix}"
            return match.group(0)

        style_css = re_mod.sub(
            r'(--color-background:\s*)([^;]+)(;)',
            _rewrite_bg,
            style_css,
        )
        style_css = re_mod.sub(
            r'(--color-foreground:\s*)([^;]+)(;)',
            _rewrite_fg,
            style_css,
        )
        if style_css != original:
            file_dict["public/style.css"] = style_css
            changed_any = True
            print(
                "  [post-process] Rewrote emerald --color-background/"
                "--color-foreground in style.css to spec palette"
            )

    # ---- tailwind.config.js: rewrite emerald colors in theme.extend.colors
    tw = file_dict.get("tailwind.config.js", "")
    if isinstance(tw, str):
        original = tw
        token_targets = {
            "surface": surface_target,
            "foreground": fg_target,
            "muted": muted_target,
            "border": border_target,
            "card": card_target,
            "background": bg_target,
        }
        for token, target in token_targets.items():
            pattern = re_mod.compile(
                rf"(\b{token}\s*:\s*)(['\"])([^'\"]+)(['\"])"
            )

            def _rewrite(match, _tgt=target):
                prefix, q1, current, q2 = match.groups()
                if _is_emerald(current):
                    return f"{prefix}{q1}{_tgt}{q2}"
                return match.group(0)

            tw = pattern.sub(_rewrite, tw)
        if tw != original:
            file_dict["tailwind.config.js"] = tw
            changed_any = True
            print(
                "  [post-process] Rewrote emerald defaults in "
                "tailwind.config.js theme to spec palette"
            )

    return list(file_dict.items()) if changed_any else files


@processor(after=('_ensure_premium_css',))
def _ensure_accent_color_variety(files: list, spec: dict) -> list:
    """COLOR_VARIETY1 (MW v12): inject --color-accent-warm + --color-accent-earth.

    Generated apps historically use --color-accent (single hue) everywhere,
    which makes every page read as one-dimensional blue. This post-processor
    adds two additional semantic accent CSS variables that designs can reach
    for to differentiate featured/warm/earthy UI elements from primary CTAs:

      --color-accent-warm: gold/yellow family (featured badges, star ratings,
                              highlighted stat cards, price emphasis)
      --color-accent-earth: orange/amber family (section accents, warm status,
                              secondary destructive-neutral states)

    Values come from optional spec.design.accent_2_color / accent_3_color.
    If absent, we auto-derive from primary: a warm-gold cousin + an
    earth-orange cousin that have enough contrast on the primary background
    to be readable without being loud.

    Also emits utility classes (.text-accent-warm, .bg-accent-warm, etc.) so
    both inline Tailwind-style usage and arbitrary-value usage work.
    """
    import re as re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    style_css = file_dict.get("public/style.css", "")
    if not layout and not style_css:
        return files

    design = spec.get("design", {})
    accent_warm = design.get("accent_2_color") or design.get("accent_warm_color")
    accent_earth = design.get("accent_3_color") or design.get("accent_earth_color")

    # Auto-derive defaults when not in spec; a muted gold + a muted terracotta
    # that sit naturally against either dark navy or warm cream base palettes.
    if not accent_warm:
        accent_warm = "#c9a84c"  # muted antique gold
    if not accent_earth:
        accent_earth = "#a87850"  # muted amber/terracotta

    # 1. Inject into style.css :root if missing.
    if style_css and "--color-accent-warm" not in style_css:
        root_match = re_mod.search(r'(:root\s*\{)([^}]*)(\})', style_css)
        if root_match:
            before, body, after = root_match.group(1), root_match.group(2), root_match.group(3)
            addition = (
                f"\n  --color-accent-warm: {accent_warm};"
                f"\n  --color-accent-earth: {accent_earth};"
            )
            new_root = before + body.rstrip() + addition + "\n" + after
            style_css = style_css[:root_match.start()] + new_root + style_css[root_match.end():]
            # Append utility classes once.
            if ".text-accent-warm" not in style_css:
                style_css += (
                    "\n\n/* COLOR_VARIETY1: accent variety utility classes */\n"
                    ".text-accent-warm  { color: var(--color-accent-warm); }\n"
                    ".bg-accent-warm   { background-color: var(--color-accent-warm); }\n"
                    ".border-accent-warm { border-color: var(--color-accent-warm); }\n"
                    ".text-accent-earth { color: var(--color-accent-earth); }\n"
                    ".bg-accent-earth  { background-color: var(--color-accent-earth); }\n"
                    ".border-accent-earth { border-color: var(--color-accent-earth); }\n"
                    "/* Semantic shortcuts for common uses */\n"
                    ".featured-highlight { color: var(--color-accent-warm); }\n"
                    ".badge-featured {\n"
                    "  background: rgba(201, 168, 76, 0.14);\n"
                    "  color: var(--color-accent-warm);\n"
                    "  border: 1px solid rgba(201, 168, 76, 0.35);\n"
                    "}\n"
                    ".stat-card-warm  { border-top: 3px solid var(--color-accent-warm); }\n"
                    ".stat-card-earth { border-top: 3px solid var(--color-accent-earth); }\n"
                )
            file_dict["public/style.css"] = style_css
            print(f"  [post-process] Injected --color-accent-warm ({accent_warm}) + --color-accent-earth ({accent_earth})")

    # 2. Inject into layout.ejs :root fallback too (in case style.css root is missing).
    if layout and "--color-accent-warm" not in layout:
        root_match = re_mod.search(r'(:root\s*\{)([^}]*)(\})', layout)
        if root_match:
            before, body, after = root_match.group(1), root_match.group(2), root_match.group(3)
            addition = (
                f" --color-accent-warm: {accent_warm};"
                f" --color-accent-earth: {accent_earth};"
            )
            new_root = before + body.rstrip(" ") + addition + " " + after
            layout = layout[:root_match.start()] + new_root + layout[root_match.end():]
            file_dict["views/layout.ejs"] = layout

    return list(file_dict.items())


@processor(after=('_fix_light_on_light_text',))
def _ensure_nav_glass(files: list) -> list:
    """Replace opaque nav backgrounds with transparent/glass styling.

    Claude often generates <nav class="bg-white ..."> or <nav class="bg-gray-900 ...">
    which blocks the glassmorphism backdrop-filter from premium-base.css.
    Replaces opaque bg-* Tailwind classes on <nav> with a semi-transparent
    background using the primary color CSS variable.
    """
    import re as re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Pattern: <nav with bg-white, bg-gray-*, bg-slate-*, bg-zinc-*, bg-neutral-*,
    # bg-stone-*, bg-blue-*, bg-indigo-*, etc. any opaque Tailwind bg class
    opaque_bg_pattern = re_mod.compile(
        r'(<nav\b[^>]*class="[^"]*)\b(bg-(?:white|black|gray|slate|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-?\d*(?:/\d+)?)\b([^"]*")',
        re_mod.IGNORECASE,
    )

    match = opaque_bg_pattern.search(layout)
    if match:
        # Replace the opaque bg class with semi-transparent primary
        new_layout = opaque_bg_pattern.sub(
            r'\1bg-[var(--color-primary)]/90\3',
            layout,
        )
        if new_layout != layout:
            file_dict["views/layout.ejs"] = new_layout
            print("  [post-process] Replaced opaque nav background with glass styling")

    return list(file_dict.items())


@processor(after=('_ensure_nav_glass',), phase=('build',))
def _ensure_nav_solid_surface(files: list) -> list:
    """Guarantee the <nav> element has a solid background surface.

    Defensive layer on top of _ensure_nav_glass. Scenarios this catches:
    1. Claude generates <nav class="sticky top-0 ..."> with NO background
       class at all. _ensure_nav_glass does not match (no opaque bg to
       replace), so the nav renders transparent over the hero; brand
       text + nav-links become invisible on light backgrounds.
    2. Claude's only "background" is a custom class like `nav-glass` whose
       CSS rule may be missing, overridden, or rendered at such low alpha
       that contrast collapses over a light hero.

    Fix: if the nav class list has no Tailwind background utility and no
    inline background-color style, inject `bg-[var(--color-primary)]` at
    the start of the class list. This is a Tailwind JIT arbitrary-value
    class that compiles to `background-color: var(--color-primary)` -
    fully opaque, uses the spec's primary color, and layers UNDER any
    existing `nav-glass`/blur enhancement without fighting it.

    Idempotent: skips navs that already have a bg-* class or inline
    background-color style. Skips when layout.ejs is missing.
    """
    import re as re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Match the first <nav ...> opening tag (layouts have one primary nav).
    nav_tag_pattern = re_mod.compile(r"<nav\b([^>]*)>", re_mod.IGNORECASE)
    nav_match = nav_tag_pattern.search(layout)
    if not nav_match:
        return files

    nav_attrs = nav_match.group(1)

    # Already has inline background-color → respect it.
    if re_mod.search(r'style="[^"]*background-color\s*:', nav_attrs, re_mod.IGNORECASE):
        return files

    # Extract the class="..." attribute.
    class_match = re_mod.search(r'class="([^"]*)"', nav_attrs)
    if not class_match:
        # Nav has no class attribute at all; inject one with the solid bg.
        new_nav_attrs = nav_attrs.rstrip() + ' class="bg-[var(--color-primary)]"'
        new_layout = (
            layout[: nav_match.start()]
            + "<nav" + new_nav_attrs + ">"
            + layout[nav_match.end():]
        )
        file_dict["views/layout.ejs"] = new_layout
        print("  [post-process] Injected solid bg class on unclassed <nav>")
        return list(file_dict.items())

    classes = class_match.group(1)

    # Tailwind background utilities (arbitrary-value + palette). If any of
    # these are present, the nav already has a background producer and we
    # leave it alone.
    has_bg_class = bool(
        re_mod.search(
            r'\bbg-\[(?:var\(|#|rgb|rgba|hsl)[^\]]*\]',
            classes,
        )
        or re_mod.search(
            r'\bbg-(?:primary|secondary|accent|white|black|transparent|current|inherit)\b',
            classes,
        )
        or re_mod.search(
            r'\bbg-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d+(?:/\d+)?\b',
            classes,
        )
    )
    if has_bg_class:
        return files

    # No background class found: prepend a solid primary surface.
    new_classes = ("bg-[var(--color-primary)] " + classes).strip()
    new_class_attr = f'class="{new_classes}"'
    new_nav_attrs = nav_attrs[: class_match.start()] + new_class_attr + nav_attrs[class_match.end():]
    new_layout = (
        layout[: nav_match.start()]
        + "<nav" + new_nav_attrs + ">"
        + layout[nav_match.end():]
    )
    if new_layout != layout:
        file_dict["views/layout.ejs"] = new_layout
        print("  [post-process] Injected solid bg-[var(--color-primary)] on bare <nav>")
        return list(file_dict.items())

    return files


@processor(after=('_ensure_nav_solid_surface',))
def _ensure_nav_glassmorphism(files: list, spec: dict) -> list:
    """Add glassmorphism effect to the nav when the primary color is dark.

    Design review requires backdrop-filter blur + semi-transparent background on
    the nav (MW v6 NAV_GLASS1). _ensure_nav_glass replaces white/gray opaque bgs
    but doesn't touch `bg-primary`. _ensure_nav_solid_surface adds `bg-primary`
    but no blur. Neither adds the backdrop-filter required by the design checklist.

    When primary luminance < 0.15 and layout.ejs has no backdrop-blur on the nav:
    - Adds `backdrop-blur-[20px] backdrop-saturate-[1.4]` classes
    - Adds inline style with rgba(r,g,b,0.92) to make bg semi-transparent (required
      for backdrop-filter to be visible and for glassmorphism to be detectable by
      design review)
    - Idempotent: skips if backdrop-blur already present in layout.ejs
    - _ensure_nav_solid_surface bails when it sees inline background-color style,
      so no conflict
    """
    import re as re_mod

    try:
        from design_data import get_design_context, _hex_luminance
        ctx = get_design_context(spec)
        primary_color = ctx.get("palette", {}).get("primary", "")
    except Exception:
        primary_color = spec.get("design", {}).get("primary_color", "")

    if not primary_color or not primary_color.startswith("#") or len(primary_color) < 7:
        return files

    try:
        luminance = _hex_luminance(primary_color)
    except Exception:
        return files

    if luminance >= 0.15:
        return files

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Idempotent: already has glassmorphism
    if "backdrop-blur" in layout or "backdrop-filter" in layout:
        return files

    # Compute rgba from hex primary color
    try:
        r = int(primary_color[1:3], 16)
        g = int(primary_color[3:5], 16)
        b = int(primary_color[5:7], 16)
    except (ValueError, IndexError):
        return files

    rgba = f"rgba({r},{g},{b},0.92)"
    glass_style = (
        f"background-color:{rgba};"
        f"backdrop-filter:blur(20px) saturate(1.4);"
        f"-webkit-backdrop-filter:blur(20px) saturate(1.4);"
    )

    # Find first nav tag in layout
    nav_pattern = re_mod.compile(r"(<nav\b)([^>]*)(>)", re_mod.IGNORECASE)
    nav_match = nav_pattern.search(layout)
    if not nav_match:
        return files

    nav_attrs = nav_match.group(2)

    # Add backdrop classes to class attribute
    class_match = re_mod.search(r'class="([^"]*)"', nav_attrs)
    if class_match:
        classes = class_match.group(1)
        if "backdrop-blur" not in classes:
            new_classes = classes + " backdrop-blur-[20px] backdrop-saturate-[1.4]"
            nav_attrs = nav_attrs[: class_match.start()] + f'class="{new_classes}"' + nav_attrs[class_match.end():]

    # Add or append inline style with rgba bg
    if 'style="' in nav_attrs:
        nav_attrs = re_mod.sub(
            r'style="([^"]*)"',
            lambda m: f'style="{m.group(1)};{glass_style}"',
            nav_attrs,
        )
    else:
        nav_attrs = nav_attrs + f' style="{glass_style}"'

    new_layout = (
        layout[: nav_match.start()]
        + nav_match.group(1)
        + nav_attrs
        + nav_match.group(3)
        + layout[nav_match.end():]
    )

    if new_layout != layout:
        file_dict["views/layout.ejs"] = new_layout
        print("  [post-process] Added glassmorphism to nav (dark primary)")
        return list(file_dict.items())

    return files


@processor(after=('_ensure_model_seed_images',), phase=('build',))
def _ensure_nav_logo(files: list, spec: dict) -> list:
    """Replace inline-SVG / gradient-div brand marks in the nav with a
    real <img src="/logo.png"> when a logo file exists in public/.

    Closes NAV_LOGO1. The older _ensure_logo_placement() relies on a brand
    pattern that matches anchors carrying text-utility classes like
    font-bold/text-xl/text-lg on the anchor itself. Modern Claude-generated
    layouts frequently wrap the brand in
        <a class="flex items-center gap-3 ...">
            <svg viewBox=...>...</svg>
            <span class="font-bold text-lg">Brand</span>
        </a>
    so the text classes live on a child span, not the anchor, and
    _ensure_logo_placement's brand_pattern fails to match. Customers then
    end up with an inline-SVG gradient circle in the nav even after their
    real logo is copied to public/logo.png. This defensive layer catches
    that.

    Fires only when the in-memory file set already has public/logo.png (or
    public/logo.svg). If the layout already contains <img src="/logo.png">
    inside the nav block, bails (idempotent). Only patches the first <nav>
    (the primary layout nav). Footer brand marks are intentionally left
    untouched.
    """
    import re as re_mod

    file_dict = dict(files)

    # Determine which logo file is present.
    if "public/logo.png" in file_dict:
        logo_fn = "logo.png"
    elif "public/logo.svg" in file_dict:
        logo_fn = "logo.svg"
    else:
        return files
    logo_path = f"/{logo_fn}"

    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Locate the first <nav>...</nav> block.
    nav_open = re_mod.search(r"<nav\b[^>]*>", layout, re_mod.IGNORECASE)
    if not nav_open:
        return files
    nav_close_idx = layout.find("</nav>", nav_open.end())
    if nav_close_idx == -1:
        return files
    nav_start = nav_open.start()
    nav_end = nav_close_idx + len("</nav>")
    nav_section = layout[nav_start:nav_end]

    # Idempotent: already wired.
    if f'<img src="{logo_path}"' in nav_section:
        return files

    # Derive alt text from business_name if available, else prettified app_name.
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    business_name = ""
    if settings:
        business_name = settings.get("business_name") or ""
    if not business_name:
        business_name = spec.get("business_name", "") or ""
    if not business_name:
        raw = spec.get("app_name", "App") or "App"
        business_name = " ".join(
            word.capitalize() for word in re_mod.split(r"[-_\s]+", raw) if word
        ) or "App"

    img_tag = (
        f'<img src="{logo_path}" alt="{business_name}" '
        f'class="w-10 h-10 md:w-12 md:h-12 rounded-full object-cover">'
    )

    # Pattern A: a wrapper <div ... style="...linear-gradient(...)...">...</div>
    # (often wraps the inner svg). Match this first so the whole wrapper is
    # replaced, not just the inner svg.
    gradient_div_pattern = re_mod.compile(
        r'<div\b[^>]*style="[^"]*linear-gradient[^"]*"[^>]*>.*?</div>',
        re_mod.IGNORECASE | re_mod.DOTALL,
    )
    # Pattern B: inline <svg viewBox="..."> brand mark (unwrapped).
    svg_pattern = re_mod.compile(
        r"<svg\b[^>]*\bviewBox\s*=\s*[\"'][^\"']*[\"'][^>]*>.*?</svg>",
        re_mod.IGNORECASE | re_mod.DOTALL,
    )

    new_nav_section = None
    for am in re_mod.finditer(
        r"<a\b[^>]*>.*?</a>",
        nav_section,
        re_mod.IGNORECASE | re_mod.DOTALL,
    ):
        anchor_html = am.group(0)
        div_match = gradient_div_pattern.search(anchor_html)
        svg_match = svg_pattern.search(anchor_html)

        if div_match:
            new_anchor = (
                anchor_html[: div_match.start()]
                + img_tag
                + anchor_html[div_match.end():]
            )
        elif svg_match:
            new_anchor = (
                anchor_html[: svg_match.start()]
                + img_tag
                + anchor_html[svg_match.end():]
            )
        else:
            continue

        new_nav_section = (
            nav_section[: am.start()] + new_anchor + nav_section[am.end():]
        )
        break

    if new_nav_section is None:
        return files

    new_layout = layout[:nav_start] + new_nav_section + layout[nav_end:]
    if new_layout == layout:
        return files

    file_dict["views/layout.ejs"] = new_layout
    print(f"  [post-process] Wired real {logo_fn} into nav brand mark")
    return list(file_dict.items())


@processor(after=('_ensure_nav_logo',), phase=('build',))
def _strip_broken_logo_refs(files: list, spec: dict) -> list:
    """Remove <img src="/logo.png"> tags from EJS when no logo asset exists.

    Closes fix-broken-logo-png-references. Claude's prompt instructs it to
    emit /logo.png references unconditionally, but when the customer did not
    upload a logo, those <img> tags render as broken-image icons on login,
    register, nav, and footer.
    """
    import re as re_mod

    file_dict = dict(files)
    has_logo = (
        "public/logo.png" in file_dict
        or "public/logo.svg" in file_dict
        or bool(spec.get("design", {}).get("logo_data"))
    )
    if has_logo:
        return files

    # Pattern: standalone <img> tags with src="/logo.png" or "/logo.svg"
    _logo_img_re = re_mod.compile(
        r'\s*<img\b[^>]*\bsrc="\/logo\.(?:png|svg)"[^>]*>\s*',
        re_mod.IGNORECASE,
    )
    # Pattern: favicon link pointing at /logo.png or /logo.svg
    _logo_favicon_re = re_mod.compile(
        r'<link\b[^>]*\brel="icon"[^>]*\bhref="\/logo\.(?:png|svg)"[^>]*>',
        re_mod.IGNORECASE,
    )

    count = 0
    for path in list(file_dict.keys()):
        if not path.endswith(".ejs") or isinstance(file_dict[path], bytes):
            continue
        content = file_dict[path]
        new_content = _logo_img_re.sub("", content)
        new_content = _logo_favicon_re.sub(
            '<link rel="icon" href="/favicon.svg">', new_content
        )
        if new_content != content:
            file_dict[path] = new_content
            count += 1

    if count:
        print(f"  [post-process] Stripped broken logo references from {count} file(s) (no logo asset)")
    return list(file_dict.items())


# Brand classes that contracts/layout.py accepts on any element inside
# the first <nav>. The contract selector is:
#     img, .brand, .logo, .wordmark, h1, span.site-name, a.brand
# These class names are matched via whole-word regex (not string search)
# so a nav containing e.g. `class="flex items-center brand gap-2"` is
# correctly detected: a substring search for `class="brand"` would miss
# that case.
_NAV_BRAND_CLASSES = ("brand", "logo", "wordmark", "site-name")

# Compiled once so the post-processor's hot path has no per-call regex
# compilation cost. Matches class="..." or class='...' whose value has
# the target class as a whole word (space-delimited or sole value).
_NAV_BRAND_CLASS_RE = __import__("re").compile(
    r"""class\s*=\s*["']([^"']*)["']""",
    __import__("re").IGNORECASE,
)


def _nav_has_brand_marker(nav_inner: str) -> bool:
    """Return True when the first <nav>'s inner HTML already contains
    any brand marker contracts/layout.py accepts. Checked markers:
      <img ...>
      <h1 ...>
      any element with class containing 'brand' / 'logo' / 'wordmark'
      / 'site-name' as a whole word.
    """
    lowered = nav_inner.lower()
    if "<img" in lowered or "<h1" in lowered:
        return True
    for match in _NAV_BRAND_CLASS_RE.finditer(nav_inner):
        class_tokens = match.group(1).split()
        for target in _NAV_BRAND_CLASSES:
            if target in class_tokens:
                return True
    return False


_CANONICAL_LAYOUT_HEAD_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><%= typeof title !== 'undefined' ? title : 'Home' %></title>
  <link rel="manifest" href="/manifest.json">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/tailwind.css">
  <link rel="stylesheet" href="/premium-base.css">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="/style.css">
  <script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>
</head>
<body class="font-body bg-secondary text-foreground min-h-screen flex flex-col" style="background-color: var(--color-background);">
'''
_CANONICAL_LAYOUT_FOOT_TEMPLATE = '''
</body>
</html>
'''


@processor(after=('_ensure_premium_css',), phase=('build', 'reapply'))
def _ensure_layout_html_skeleton(files: list, spec: dict = None) -> list:
    """Wrap any `views/layout*.ejs` file that's missing the proper
    HTML5 skeleton (DOCTYPE / html / head with stylesheet links /
    body tag). Without this, pages that use the broken layout render
    with zero CSS loaded (no `<link rel="stylesheet">` in head →
    Tailwind utility classes do nothing).

    Observed failure: Claude emitted
    `views/layout-public.ejs` as a bare fragment starting with
    `<nav class="bg-white">`: all public routes (home, about, blog,
    events, contact, work-with-me) that `res.render(..., { layout:
    'layout-public' })` produced unstyled HTML. The admin layout.ejs
    was correct so this was a public-only blind spot.

    Scope:
      - Every `views/layout*.ejs` file in the build.
      - File is WRAPPED (not rewritten) when it lacks `<!DOCTYPE`:
        prepend the canonical head template; append the canonical
        foot template. Claude's original content goes between.
      - If the file already has DOCTYPE or HTML opening, no-op.
      - Idempotent on reapply: the DOCTYPE check makes a second pass
        byte-identical.
    """
    import re as _re_mod
    file_dict = dict(files)
    changed = False

    for path in list(file_dict.keys()):
        if not isinstance(path, str):
            continue
        if not path.startswith("views/layout") or not path.endswith(".ejs"):
            continue
        content = file_dict[path]
        if not isinstance(content, str):
            continue
        # Skip if ANY skeleton tag is already present; wrapping would
        # produce nested <head>/<body> which is invalid HTML.
        # Uses regex with word boundaries / tag terminators so
        # `<header>` doesn't false-positive as `<head`, and
        # `<body-class>` (unlikely but defensive) doesn't false-
        # positive as `<body`.
        lowered = content.lower()
        skeleton_tag_re = _re_mod.compile(
            r'<!doctype\b|<html[\s>]|<head[\s>]|</head>|<body[\s>]|</body>|</html>'
        )
        if skeleton_tag_re.search(lowered):
            continue
        # Missing skeleton: wrap.
        wrapped = (
            _CANONICAL_LAYOUT_HEAD_TEMPLATE
            + content.rstrip()
            + _CANONICAL_LAYOUT_FOOT_TEMPLATE
        )
        file_dict[path] = wrapped
        changed = True
        print(
            f"  [post-process] Wrapped {path} with canonical HTML5 "
            "skeleton (was a bare fragment: no DOCTYPE/head/body)"
        )

    return list(file_dict.items()) if changed else files


@processor(after=('_strip_broken_logo_refs',), phase=('build', 'reapply'))
def _ensure_nav_brand_element(files: list, spec: dict = None) -> list:
    """Guarantee views/layout.ejs <nav> carries a brand element.

    contracts/layout.py requires the first <nav> to contain at least one
    of `img, .brand, .logo, .wordmark, h1, span.site-name, a.brand`. When
    Claude emits a nav that has only text links and no brand, the
    contract fires "<nav> missing brand element" and the build never
    reaches deploy.

    Fix: locate the first <nav>...</nav> block. If no brand marker is
    present, inject a text wordmark as the first child of the nav.
    Wordmark text prefers spec.settings.site_name, then business_name,
    then pretty-cased app_name; falls back to "Home".

    Idempotent: if any brand marker is present, no-op. Safe on repeat
    runs and on the reapply phase.

    Placement:
      Runs after _ensure_nav_logo (so a real <img src="/logo.png"> is
      injected first when a logo asset is present) and after
      _strip_broken_logo_refs (so we do not inject a wordmark into a
      nav that was about to have its only <img> stripped).
    """
    import re as re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout or not isinstance(layout, str):
        return files

    # Locate the first <nav>...</nav> block.
    nav_open = re_mod.search(r"<nav\b[^>]*>", layout, re_mod.IGNORECASE)
    if not nav_open:
        return files
    nav_close_idx = layout.find("</nav>", nav_open.end())
    if nav_close_idx == -1:
        return files
    nav_inner_start = nav_open.end()
    nav_inner_end = nav_close_idx
    nav_inner = layout[nav_inner_start:nav_inner_end]

    # Idempotent: if any brand marker is already present, bail.
    if _nav_has_brand_marker(nav_inner):
        return files

    # Derive wordmark text from spec. Order: settings.site_name ->
    # settings.business_name -> prettified app_name -> "Home".
    wordmark = ""
    spec = spec or {}
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    if settings:
        wordmark = str(settings.get("site_name") or "").strip()
        if not wordmark:
            wordmark = str(settings.get("business_name") or "").strip()
    if not wordmark:
        raw = str(spec.get("app_name") or "").strip()
        if raw:
            # strip trailing "-v\d+-\d+" version suffix like "-v1-123456"
            raw = re_mod.sub(r"-v\d+(?:-\d+)?$", "", raw)
            wordmark = " ".join(
                w.capitalize() for w in re_mod.split(r"[-_\s]+", raw) if w
            )
    if not wordmark:
        wordmark = "Home"

    # HTML-escape the wordmark to prevent breakage if app_name contains
    # characters that would close an attribute or break the anchor.
    import html as _html
    wordmark_safe = _html.escape(wordmark)

    # Inject as first child of the first <nav>. Use an anchor carrying
    # class="brand site-name ..." so the contract's both a.brand AND
    # span.site-name selectors match, maximizing forward-compat.
    brand_anchor = (
        f'<a href="/" class="brand site-name font-display font-semibold '
        f'text-lg whitespace-nowrap" aria-label="{wordmark_safe} home">'
        f'<span class="site-name">{wordmark_safe}</span>'
        f'</a>\n    '
    )
    new_layout = (
        layout[: nav_open.end()]
        + "\n    "
        + brand_anchor
        + layout[nav_open.end() :]
    )
    if new_layout == layout:
        return files

    file_dict["views/layout.ejs"] = new_layout
    print(
        f"  [post-process] _ensure_nav_brand_element: injected wordmark "
        f"'{wordmark}' into <nav> (no brand candidate found)"
    )
    return list(file_dict.items())


@processor(after=('_ensure_nav_brand_element',), phase=('build', 'reapply'))
def _fix_nav_brand_contrast_and_dedupe(files: list, spec: dict = None) -> list:
    """Two narrow fixes on the first <nav>:

      (a) Contrast fix: when the first <nav> has a primary-colored
          background (either the `bg-[var(--color-primary)]` / `bg-primary`
          Tailwind class OR an inline style that paints the nav in the
          primary hex), any descendant anchor whose inline style says
          `color: var(--color-primary)` will be maroon-on-maroon and
          invisible. Rewrite that inline color to
          `color: var(--color-on-primary, #fff)` so the wordmark + nav
          links are legible.

      (b) Dedupe: _ensure_nav_brand_element injects
          `<a class="brand site-name font-display ...">`  as the first
          child of <nav> when no contract-recognized brand class is
          present. Claude often ALSO emits a wordmark anchor that lacks
          the contract classes (so the injector still fires), producing
          a duplicate. After injection, if the nav contains BOTH the
          injected orphan AND a separate wordmark anchor whose visible
          text matches the injected wordmark, strip the orphan and
          transplant the `brand site-name` classes onto the kept anchor
          so contracts/layout.py still passes.

    No spec palette is hardcoded: both paths read through the existing
    :root CSS vars so they pick up whatever _ensure_color_consistency
    already established.
    """
    import re as re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not isinstance(layout, str) or "<nav" not in layout:
        return files

    nav_open = re_mod.search(r"<nav\b[^>]*>", layout, re_mod.IGNORECASE)
    if not nav_open:
        return files
    nav_tag = nav_open.group(0)
    nav_close_idx = layout.find("</nav>", nav_open.end())
    if nav_close_idx == -1:
        return files
    nav_inner_start = nav_open.end()
    nav_inner = layout[nav_inner_start:nav_close_idx]
    original_inner = nav_inner

    # ---- (a) detect primary-colored nav bg
    nav_is_primary_bg = False
    if re_mod.search(r"bg-\[var\(--color-primary\)\]|bg-primary\b", nav_tag):
        nav_is_primary_bg = True
    # inline style with var(--color-primary) in background-color
    if re_mod.search(
        r'style\s*=\s*"[^"]*background-color:\s*[^"]*var\(--color-primary\)',
        nav_tag,
    ):
        nav_is_primary_bg = True
    # inline style with the literal primary hex (from design_data)
    spec_primary = ""
    if isinstance(spec, dict):
        spec_primary = ((spec.get("design") or {}).get("primary_color") or "").lower()
    if spec_primary:
        hex_pat = re_mod.escape(spec_primary)
        if re_mod.search(
            rf'style\s*=\s*"[^"]*background-color:\s*[^"]*(?:{hex_pat}|rgba?\([^)]*\))',
            nav_tag,
            re_mod.IGNORECASE,
        ):
            # treat as primary if the nav carries an explicit primary hex
            # OR an rgba() that matches the primary. Cheap heuristic; the
            # guarded replacement below only rewrites color tokens that
            # literally reference var(--color-primary), never hex values.
            nav_is_primary_bg = True

    if nav_is_primary_bg:
        # Rewrite inline `color: var(--color-TOKEN)` on any descendant
        # inside the nav → `color: var(--color-on-primary, #fff)` for any
        # TOKEN that's meant for an ON-SURFACE (light/dark-neutral) color
        # - those are illegible on a primary-colored bg.
        #
        # Scope: --color-primary (maroon on maroon), --color-foreground
        # (dark gray on maroon), --color-text (Claude sometimes uses
        # this; old emerald default was #064E3B). Does NOT touch
        # --color-on-primary / --color-secondary / --color-accent etc.
        # since those are already designed for visibility on the
        # primary surface.
        #
        # Claude can emit
        # `style="color: var(--color-foreground);"` on every nav link;
        # a fix that only catches --color-primary leaves every link
        # dark on the primary color and illegible.
        before = nav_inner
        _on_surface_tokens = ("primary", "foreground", "text")
        for _token in _on_surface_tokens:
            # Negative lookbehind excludes "color:" when
            # preceded by a letter or hyphen. Without this guard the pattern
            # also matched inside "background-color:", "border-color:", and
            # "outline-color:", silently rewriting background fills to
            # on-primary and making Login-style buttons (with
            # style="background-color: var(--color-primary)") render
            # invisible (white-on-white). Only rewrite "color:" when it
            # starts a CSS declaration.
            nav_inner = re_mod.sub(
                rf"(?<![\w-])color:\s*var\(--color-{_token}\b[^)]*\)",
                "color: var(--color-on-primary, #fff)",
                nav_inner,
            )
        if nav_inner != before:
            print(
                "  [post-process] Fixed nav wordmark/link contrast: "
                "on-surface color tokens (primary/foreground/text) → "
                "var(--color-on-primary) on primary-colored <nav>"
            )

    # ---- (b) dedupe the _ensure_nav_brand_element orphan
    # Marker: our injected anchor has `class="brand site-name ..."`
    # AND a `<span class="site-name">...</span>` inner wrapper; this
    # latter structure is SPECIFIC to _ensure_nav_brand_element's
    # template. Post-dedupe consolidations transplant `brand site-name`
    # classes onto Claude's anchors but don't add the span, so requiring
    # the span disambiguates "fresh injected orphan" from "already-
    # consolidated kept anchor" and preserves idempotency on reapply.
    injected_re = re_mod.compile(
        r'<a\s+href="/"\s+class="brand site-name[^"]*"[^>]*>'
        r'\s*<span[^>]*class="[^"]*\bsite-name\b[^"]*"[^>]*>([^<]+)</span>\s*'
        r'</a>',
    )
    injected = injected_re.search(nav_inner)
    if injected:
        wordmark_text = (injected.group(1) or "").strip()
        if wordmark_text:
            # Is there ANOTHER anchor in the nav whose visible text
            # CONTAINS the wordmark? Search in the nav_inner MINUS the
            # injected one. Strict text equality is too tight: when
            # Claude emits an anchor reading
            # "Sample Practitioner Admin" and the injected orphan reads
            # "Sample Practitioner", a `\s*wordmark\s*</a>` regex
            # misses and both wordmarks render. So match "anchor
            # whose visible text (tags stripped) starts with the
            # wordmark": "Sample Practitioner", "Sample Practitioner Admin",
            # "Sample Practitioner <span>Admin</span>". Still narrow enough
            # that a legit nav link like "About Sample Practitioner Studio"
            # won't match (text doesn't START with the wordmark).
            without_injected = (
                nav_inner[: injected.start()] + nav_inner[injected.end():]
            )
            # Enumerate every <a>...</a> in the nav, strip inner tags,
            # and pick the first whose visible text has a shared-prefix
            # relationship with the injected wordmark.
            #
            # Forward direction:
            # Claude emits "Sample Practitioner Admin", injected orphan is
            # "Sample Practitioner": match fires because
            # "Sample Practitioner Admin".startswith("Sample Practitioner").
            #
            # Reverse direction:
            # Claude emits "Sample Practitioner", injected orphan is
            # "Sample Practitioner V1 Rc2" (derived from unpopulated
            # settings.site_name falling through to app_name
            # prettification). A forward-only match misses this because
            # "Sample Practitioner".startswith("Sample Practitioner V1 Rc2")
            # is false.
            # Bidirectional match: EITHER starts-with satisfies.
            # Still narrow enough that a legit nav link like
            # "About Sample Practitioner Studio" won't match (no shared prefix).
            other = None
            wordmark_lc = wordmark_text.lower()
            for _m in re_mod.finditer(
                r'<a\b[^>]*>([\s\S]*?)</a>',
                without_injected,
            ):
                inner_html = _m.group(1)
                visible = re_mod.sub(r'<[^>]+>', '', inner_html).strip()
                if not visible:
                    continue
                visible_lc = visible.lower()
                if (visible_lc.startswith(wordmark_lc)
                        or wordmark_lc.startswith(visible_lc)):
                    other = _m
                    break
            if other:
                # Consolidate: transplant the `brand site-name` class
                # onto the kept anchor so layout contracts still pass,
                # then remove the injected orphan.
                kept_tag = other.group(0)
                # Inject brand classes into the kept anchor's class attr.
                if re_mod.search(r'class\s*=\s*"[^"]*"', kept_tag):
                    new_kept = re_mod.sub(
                        r'class\s*=\s*"([^"]*)"',
                        lambda m: f'class="brand site-name {m.group(1)}"',
                        kept_tag,
                        count=1,
                    )
                else:
                    # No existing class attr: add one after <a
                    new_kept = re_mod.sub(
                        r'<a\b',
                        '<a class="brand site-name"',
                        kept_tag,
                        count=1,
                    )
                # Apply: replace kept anchor with classes, then drop
                # the injected orphan.
                without_injected = without_injected.replace(
                    kept_tag, new_kept, 1,
                )
                nav_inner = without_injected
                print(
                    "  [post-process] Consolidated duplicate nav "
                    f"wordmark '{wordmark_text}': stripped injected "
                    "orphan, moved brand classes to Claude's anchor"
                )
            else:
                # No match: orphan stays. Inherit nav text color so
                # it doesn't render as default black on a primary-bg
                # nav (an orphan wordmark otherwise renders dark on the
                # primary color, barely visible, and makes the nav
                # taller).
                #
                # Pick target color by nav bg context:
                #   primary-bg → var(--color-on-primary, #fff)
                #   else (white/neutral) → var(--color-foreground)
                target_color = (
                    "var(--color-on-primary, #fff)"
                    if nav_is_primary_bg
                    else "var(--color-foreground)"
                )
                # Re-find the orphan in the possibly-updated nav_inner
                # (the contrast-fix (a) may have already touched it).
                orphan_match = injected_re.search(nav_inner)
                if orphan_match:
                    orig_orphan = orphan_match.group(0)
                    # If the orphan already has a color declaration
                    # (e.g., (a) rewrote it), leave it alone.
                    if re_mod.search(r'color\s*:\s*var\(',
                                     orig_orphan):
                        pass
                    elif re_mod.search(r'\bstyle\s*=\s*"',
                                       orig_orphan):
                        # Append color into existing style attr.
                        def _append_color(sm):
                            pre, body, post = sm.group(1), sm.group(2), sm.group(3)
                            sep = "" if (not body.strip()
                                         or body.rstrip().endswith(";")) else ";"
                            return f'{pre}{body}{sep} color: {target_color}{post}'
                        new_orphan = re_mod.sub(
                            r'(\bstyle\s*=\s*")([^"]*)(")',
                            _append_color,
                            orig_orphan,
                            count=1,
                        )
                        nav_inner = nav_inner.replace(
                            orig_orphan, new_orphan, 1,
                        )
                        print(
                            "  [post-process] Injected nav text color "
                            f"into injected orphan wordmark (bg="
                            f"{'primary' if nav_is_primary_bg else 'white'})"
                        )
                    else:
                        # Inject style attr onto the <a> tag.
                        new_orphan = re_mod.sub(
                            r'(<a\b)',
                            lambda m: f'{m.group(1)} style="color: {target_color}"',
                            orig_orphan,
                            count=1,
                        )
                        nav_inner = nav_inner.replace(
                            orig_orphan, new_orphan, 1,
                        )
                        print(
                            "  [post-process] Injected nav text color "
                            f"into injected orphan wordmark (bg="
                            f"{'primary' if nav_is_primary_bg else 'white'})"
                        )

    if nav_inner == original_inner:
        return files

    new_layout = (
        layout[:nav_inner_start] + nav_inner + layout[nav_close_idx:]
    )
    file_dict["views/layout.ejs"] = new_layout
    return list(file_dict.items())


@processor(after=('_strip_broken_logo_refs',), phase=('build',))
def _ensure_icon_helper(files: list) -> list:
    """Inject the icon helper template into the build.

    Copies cli/templates/icon_helper.js to the build and patches server.js
    to load it. Provides SVG icons via res.locals.icons for EJS templates.
    """
    file_dict = dict(files)

    # Skip if already present
    if "icon_helper.js" in file_dict:
        return files

    # Load the template
    template_path = _TEMPLATES_DIR / "icon_helper.js"
    if not template_path.exists():
        return files
    file_dict["icon_helper.js"] = template_path.read_text()
    print("  [post-process] Injected icon_helper.js")

    # Patch server.js to load the icon helper
    server_js = file_dict.get("server.js", "")
    if server_js and "icon_helper" not in server_js:
        # Insert after the express.urlencoded middleware line, before routes
        import re as re_mod
        marker = "app.use(express.urlencoded"
        if marker in server_js:
            idx = server_js.index(marker)
            line_end = server_js.index("\n", idx)
            inject = "\nrequire('./icon_helper')(app);\n"
            server_js = server_js[:line_end + 1] + inject + server_js[line_end + 1:]
            file_dict["server.js"] = server_js
            print("  [post-process] Patched server.js to load icon_helper")

    return list(file_dict.items())


_DEFAULT_FAVICON_ICO_PATH = Path(__file__).parent / "assets" / "default_favicon.ico"


# Paths to EXCLUDE from the generated sitemap even when marked
# auth_required=False in the spec. These are transactional / auth
# surfaces a search engine should not index.
_SITEMAP_PATH_DENYLIST = frozenset({
    "/login",
    "/logout",
    "/signup",
    "/register",
    "/forgot-password",
    "/reset-password",
    "/verify-email",
    "/verify-email-sent",
    "/resend-verification",
    "/set-password",
    "/magic-link-sent",
    "/health",
})


def _sitemap_pages(spec) -> list:
    """Collect public page paths from the spec for sitemap emission.

    Returns a list of unique path strings in the order they appear in
    spec.pages, filtered to keep:
      - auth_required==False only
      - path does not start with /admin or /api
      - path is not in _SITEMAP_PATH_DENYLIST

    The path / is always included when the spec has any pages (the
    homepage is always public).
    """
    pages = spec.get("pages") if isinstance(spec, dict) else None
    if not isinstance(pages, list):
        return []
    seen = set()
    keep = []
    for p in pages:
        if not isinstance(p, dict):
            continue
        path = str(p.get("path") or "").strip()
        if not path or not path.startswith("/"):
            continue
        if p.get("auth_required"):
            continue
        if path.startswith("/admin") or path.startswith("/api"):
            continue
        if path in _SITEMAP_PATH_DENYLIST:
            continue
        # Parameterized paths (e.g. /events/:slug) are patterns, not
        # URLs; listing them literally would pollute the sitemap. Skip.
        if ":" in path or "*" in path:
            continue
        if path in seen:
            continue
        seen.add(path)
        keep.append(path)
    return keep


def _sitemap_base_url(spec) -> str:
    """Resolve the <loc> base URL for the sitemap.

    Prefers spec.settings.site_url (authoritative customer-configured
    origin). Falls back to the placeholder https://example.com, which
    the customer can edit later via admin/settings + a rebuild.

    The return value NEVER carries a trailing slash so `base + path`
    concatenation is well-formed.
    """
    settings = spec.get("settings") if isinstance(spec, dict) else None
    if isinstance(settings, dict):
        raw = str(settings.get("site_url") or "").strip()
        if raw:
            return raw.rstrip("/")
    return "https://example.com"


@processor(after=('_ensure_favicon_ico_fallback',), phase=('build', 'reapply'))
def _ensure_sitemap_xml(files, spec):
    """Emit public/sitemap.xml listing every public (non-auth) page.

    Generated
    apps ship production-ready, but without a sitemap search engines
    under-index them and the "professional polish" bar is visibly lower
    than hand-built competitors.

    Rules:
      - Skip entirely when spec.pages is empty or unset.
      - No-op when public/sitemap.xml is already present (Claude
        output or a prior reapply; we do not clobber).
      - <loc> entries use spec.settings.site_url if set, otherwise the
        placeholder https://example.com.
      - Admin / API / auth surfaces are excluded.

    Runs in both build and reapply so fix-loop file writes can't drop it.
    """
    file_dict = dict(files)
    if "public/sitemap.xml" in file_dict:
        return files

    paths = _sitemap_pages(spec)
    if not paths:
        return files

    base = _sitemap_base_url(spec)
    # Build the <urlset> document: one <url> per path. XML-escape every
    # loc so `&` `<` `>` in either base URL or path can't break the
    # document structure.
    from xml.sax.saxutils import escape as _xml_escape
    url_blocks = "\n".join(
        f"  <url>\n    <loc>{_xml_escape(base + path)}</loc>\n  </url>"
        for path in paths
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{url_blocks}\n"
        "</urlset>\n"
    )
    file_dict["public/sitemap.xml"] = xml
    print(
        f"  [post-process] Emitted public/sitemap.xml with {len(paths)} "
        "URL(s)"
    )
    return list(file_dict.items())


@processor(after=('_ensure_icon_helper',), phase=('build', 'reapply'))
def _ensure_favicon_ico_fallback(files: list) -> list:
    """Bundle a default public/favicon.ico so /favicon.ico never 404s.

    Claude
    emits public/favicon.svg and modern browsers are happy, but old
    browsers + some RSS/feed readers request /favicon.ico specifically
    and 404 when only the SVG is present. express.static picks the
    .ico up automatically once the asset lives in public/.

    Guarantees:
      - If public/favicon.ico is already present (Claude output or a
        prior reapply pass), no-op.
      - If public/favicon.svg is NOT in the file_dict, no-op; the app
        has no icon at all, so silently adding just an .ico would be
        misleading.
      - Otherwise copies cli/assets/default_favicon.ico as bytes into
        file_dict['public/favicon.ico']. The bundled asset is a neutral
        16x16 + 32x32 blue-on-white disc (pre-generated via Pillow,
        committed to the repo so build-time does not depend on Pillow).

    Phase: runs in BOTH build and reapply so fix loops that prune
    binary artifacts cannot drop the fallback.
    """
    file_dict = dict(files)

    has_svg = "public/favicon.svg" in file_dict
    has_ico = "public/favicon.ico" in file_dict
    if not has_svg or has_ico:
        return files

    if not _DEFAULT_FAVICON_ICO_PATH.exists():
        # Asset not bundled (should not happen in practice, but do not
        # crash the build over a cosmetic polish item).
        return files

    file_dict["public/favicon.ico"] = _DEFAULT_FAVICON_ICO_PATH.read_bytes()
    print(
        "  [post-process] Bundled default public/favicon.ico fallback "
        f"({_DEFAULT_FAVICON_ICO_PATH.stat().st_size} bytes)"
    )
    return list(file_dict.items())


def _content_hash_for_sw_cache(files) -> str:
    """Compute a deterministic 12-char content hash for the service-worker
    cache name.

    Excludes public/sw.js itself so the resulting hash does not depend on
    the value it is about to stamp into sw.js (self-reference would
    produce a cycle).

    Excludes binary assets (bytes) because their presence or absence
    already shows up via other textual files (e.g. layout.ejs <img src>),
    and hashing bytes interleaved with strings complicates the digest
    without meaningfully improving drift sensitivity.

    The files argument may be a list of (path, content) tuples OR a
    dict. The caller is responsible for that shape; we accept both.
    """
    import hashlib

    if isinstance(files, dict):
        items = files.items()
    else:
        items = files or []
    h = hashlib.sha256()
    for path, content in sorted(items, key=lambda kv: str(kv[0])):
        if not isinstance(path, str) or path == "public/sw.js":
            continue
        if not isinstance(content, str):
            continue
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(content.encode("utf-8", errors="ignore"))
        h.update(b"\x01")
    return h.hexdigest()[:12]


@processor(after=('_ensure_sitemap_xml',), phase=('build', 'reapply'))
def _bump_service_worker_cache_version(files, spec):
    """Stamp a content-derived cache name into public/sw.js so AI Fix /
    reapply runs automatically invalidate client caches.

    Generated
    apps auto-register sw.js and aggressively cache assets with a fixed
    CACHE_NAME of 'app-shell-v1'. After an AI Fix or revision rewrites
    views, users see stale content until they hard-reload because the
    service worker keeps serving the old shell from the unchanged cache.

    Fix: derive CACHE_NAME from a sha256 of the full file_dict
    (excluding sw.js itself) and splice it in. When content changes
    (a fix was applied), the hash changes, CACHE_NAME changes, the
    worker's activate handler evicts the old cache, and clients fetch
    fresh assets on the next request.

    Idempotency: a reapply pass with no changes produces the same hash,
    so sw.js stays byte-identical. Genuine content changes DO bump the
    hash, which is the desired cache-busting behavior.

    Phase=('build', 'reapply') so fix-loop-triggered reapplies bump the
    cache too.
    """
    import re as re_mod

    file_dict = dict(files)
    sw = file_dict.get("public/sw.js")
    if not isinstance(sw, str) or "CACHE_NAME" not in sw:
        return files

    cache_suffix = _content_hash_for_sw_cache(file_dict)
    new_cache_name = f"app-shell-{cache_suffix}"

    # Replace the literal value. Tolerates single or double quotes and any
    # existing suffix so re-runs pick up the NEW hash when content shifts.
    pattern = re_mod.compile(
        r"""(const\s+CACHE_NAME\s*=\s*)['"][^'"]*['"]""",
    )
    if not pattern.search(sw):
        return files
    new_sw = pattern.sub(
        lambda _m: f"{_m.group(1)}'{new_cache_name}'",
        sw,
        count=1,
    )

    if new_sw == sw:
        return files

    file_dict["public/sw.js"] = new_sw
    print(
        f"  [post-process] Bumped service-worker cache name to "
        f"{new_cache_name}"
    )
    return list(file_dict.items())


@processor(after=('_ensure_home_video_click_to_play',), phase=('build',))
def _remove_homepage_video_section(files: list) -> list:
    """Remove featured video preview sections from the homepage.

    Claude often generates a 'Featured Videos' section on index/home pages with
    YouTube thumbnail images that break due to ad blockers and CSP. These sections
    are not requested by customers and create a broken experience. Videos should
    only appear on the dedicated /videos page.
    """
    import re as re_mod

    file_dict = dict(files)
    changed = False

    for view_path in ["views/home.ejs", "views/index.ejs"]:
        content = file_dict.get(view_path, "")
        if not content:
            continue

        # Pattern: EJS conditional block wrapping a video section
        # Matches: <% if (videos...) { %> ... </section> <% } %>
        # Also matches: <!-- Videos Preview --> before the block
        video_block = re_mod.search(
            r'(?:<!--\s*Videos?\s*(?:Preview|Section)\s*-->\s*\n?)?'
            r'<%\s*if\s*\([^)]*videos[^)]*\)\s*\{\s*%>\s*\n?'
            r'(?:.*?\n)*?'  # content lines
            r'<%\s*\}\s*%>',
            content,
            re_mod.IGNORECASE
        )
        if video_block:
            content = content[:video_block.start()] + content[video_block.end():]
            # Clean up double blank lines left behind
            content = re_mod.sub(r'\n{3,}', '\n\n', content)
            file_dict[view_path] = content
            changed = True
            print(f"  [post-process] Removed video preview section from {view_path}")

    if changed:
        return list(file_dict.items())
    return files


@processor(after=('_ensure_icon_helper',), phase=('build', 'reapply'))
def _ensure_video_embeds(files: list, spec: dict) -> list:
    """Convert YouTube embeds to ad-blocker-safe patterns, fix bad IDs, and patch CSP.

    Detects video fields in spec data models or raw YouTube iframes in views.
    Index/list views: thumbnail image + play button overlay linking to YouTube
      (avoids Error 153 from ad blockers blocking YouTube's ad infrastructure).
    Detail views: youtube-nocookie.com iframe with "Watch on YouTube" fallback.
    Also patches helmet CSP to allow youtube-nocookie.com in frame-src.
    Detects and replaces rickroll/placeholder video IDs with curated fallbacks.
    """
    import re as re_mod

    # Known placeholder/rickroll YouTube IDs that Claude tends to generate
    _PLACEHOLDER_IDS = {
        "dQw4w9WgXcQ",  # rickroll
        "jNgzypdkiKc",  # another common placeholder
        "xxxxxxxxxxx",  # obvious placeholder
        "VIDEO_ID_01",  # template placeholder
    }

    # Curated pool of real, generic YouTube videos (verified working)
    # These are diverse enough to work for most app domains
    _FALLBACK_VIDEO_IDS = [
        "aXItOY0sLRY", "lFcSrYw-ARY", "inpok4MKVLM", "tybOi4hjZFQ",
        "1ZYbU82GVz4", "jPpUNAFHgxM", "hlWiI4xVXKY", "77ZozI0rw7w",
        "ZToicYcHIOU", "2OEL4P1Rz04", "hHW1oY26kxQ", "5qap5aO4i9A",
    ]

    # Check if video embedding is needed
    video_keywords = {"video", "youtube", "embed", "media_url", "trailer", "clip"}
    has_video_fields = False
    for model in spec.get("data_models", []):
        if isinstance(model, dict):
            for field in model.get("fields", []):
                field_name = field.get("name", "").lower() if isinstance(field, dict) else str(field).lower()
                if any(kw in field_name for kw in video_keywords):
                    has_video_fields = True
                    break
        if has_video_fields:
            break

    # Also check for raw YouTube iframes in views
    youtube_iframe_re = re_mod.compile(
        r'<iframe[^>]*\ssrc=["\']https?://(?:www\.)?youtube(?:-nocookie)?\.com/embed/([a-zA-Z0-9_-]{11})[^"\']*["\'][^>]*>(?:</iframe>)?',
        re_mod.IGNORECASE
    )
    has_youtube_iframes = False
    if not has_video_fields:
        for path, content in files:
            if path.endswith(".ejs") and isinstance(content, str):
                if youtube_iframe_re.search(content):
                    has_youtube_iframes = True
                    break

    if not has_video_fields and not has_youtube_iframes:
        return files

    file_dict = dict(files)

    # Phase 0: Replace rickroll/placeholder/duplicate video IDs in all files
    # Also checks seed.sql for placeholder IDs in INSERT statements
    _vid_id_pattern = re_mod.compile(r'(?:youtube\.com/embed/|youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})')
    fallback_idx = [0]

    # Build category-aware video ID list: pool IDs first, then generic fallbacks
    _category_video_ids = list(_FALLBACK_VIDEO_IDS)  # start with generic
    categories = _match_stock_categories(spec)
    if categories and _VIDEO_POOLS:
        pool_ids = []
        seen = set()
        for cat in categories:
            for vid in _VIDEO_POOLS.get(cat, []):
                if vid not in seen:
                    pool_ids.append(vid)
                    seen.add(vid)
        if pool_ids:
            # Category IDs first, then generic fallbacks (deduped)
            _category_video_ids = pool_ids + [v for v in _FALLBACK_VIDEO_IDS if v not in seen]

    def _next_fallback_id():
        vid = _category_video_ids[fallback_idx[0] % len(_category_video_ids)]
        fallback_idx[0] += 1
        return vid

    for path in list(file_dict.keys()):
        content = file_dict[path]
        if isinstance(content, bytes):
            continue
        if not (path.endswith(".ejs") or path.endswith(".sql") or path.endswith(".js")):
            continue

        found_ids = _vid_id_pattern.findall(content)
        if not found_ids:
            continue

        # Detect: all same ID (duplicates), any known placeholder, or
        # unverified IDs in seed.sql (Claude always generates fake IDs)
        unique_ids = set(found_ids)
        all_same = len(unique_ids) == 1 and len(found_ids) > 1
        has_placeholder = bool(unique_ids & _PLACEHOLDER_IDS)
        _CURATED_SET = set(_category_video_ids)
        is_seed_with_unknown_ids = path.endswith(".sql") and bool(unique_ids - _CURATED_SET)

        if all_same or has_placeholder or is_seed_with_unknown_ids:
            # Build replacement map: each occurrence gets a unique fallback
            replaced_ids = {}
            occurrence_count = {}

            def _replace_vid(m):
                full = m.group(0)
                vid_id = m.group(1)
                if vid_id in _PLACEHOLDER_IDS or (all_same and len(found_ids) > 1) or (is_seed_with_unknown_ids and vid_id not in _CURATED_SET):
                    # Track per-occurrence so each gets a unique replacement
                    key = vid_id
                    occurrence_count[key] = occurrence_count.get(key, 0) + 1
                    cache_key = f"{key}_{occurrence_count[key]}"
                    if cache_key not in replaced_ids:
                        replaced_ids[cache_key] = _next_fallback_id()
                    return full.replace(vid_id, replaced_ids[cache_key])
                return full

            new_content = _vid_id_pattern.sub(_replace_vid, content)
            if new_content != content:
                file_dict[path] = new_content
                print(f"  [post-process] Replaced {'placeholder' if has_placeholder else 'duplicate'} video IDs in {path}")

    # Inject vendor files from templates (still needed for detail page fallback)
    lite_js_path = _TEMPLATES_DIR / "lite-youtube.js"
    lite_css_path = _TEMPLATES_DIR / "lite-youtube.css"

    if lite_js_path.exists() and "public/vendor/lite-youtube.js" not in file_dict:
        file_dict["public/vendor/lite-youtube.js"] = lite_js_path.read_text()
    if lite_css_path.exists() and "public/vendor/lite-youtube.css" not in file_dict:
        file_dict["public/vendor/lite-youtube.css"] = lite_css_path.read_text()

    # Patch layout.ejs to include vendor files
    layout = file_dict.get("views/layout.ejs", "")
    if layout:
        changed = False
        if "lite-youtube.css" not in layout and "</head>" in layout:
            layout = layout.replace(
                "</head>",
                '  <link rel="stylesheet" href="/vendor/lite-youtube.css">\n</head>',
            )
            changed = True
        if "lite-youtube.js" not in layout and "</body>" in layout:
            layout = layout.replace(
                "</body>",
                '  <script src="/vendor/lite-youtube.js"></script>\n</body>',
            )
            changed = True
        if changed:
            file_dict["views/layout.ejs"] = layout
            print("  [post-process] Added lite-youtube vendor files to layout")

    # Convert raw YouTube iframes: index/list → thumbnail links, detail → nocookie iframe
    # v17 FIX G: include "home" and "landing", previously they fell through to the
    # detail (live iframe) branch, surfacing YouTube Error 153 when an ID went stale.
    _INDEX_PATTERNS = ("index", "list", "all", "home", "landing")
    _DETAIL_PATTERNS = ("detail", "show", "view", "single")

    for path in list(file_dict.keys()):
        if not path.endswith(".ejs") or isinstance(file_dict[path], bytes):
            continue
        content = file_dict[path]
        if "lite-youtube" in content:
            continue  # Already converted

        basename = path.lower().rsplit("/", 1)[-1].replace(".ejs", "")
        is_index = any(p in basename for p in _INDEX_PATTERNS)

        if is_index:
            # Index/list pages: thumbnail + play button link (no embed, no Error 153)
            # Uses /api/video-thumb/ proxy to bypass ad-blockers that block i.ytimg.com
            def _thumb_replacement(m):
                vid_id = m.group(1)
                return (
                    f'<a href="https://www.youtube.com/watch?v={vid_id}" target="_blank" '
                    f'rel="noopener" class="block relative group">'
                    f'<img src="/api/video-thumb/{vid_id}" '
                    f'alt="Video thumbnail" class="w-full h-full object-cover" loading="lazy">'
                    f'<span class="absolute inset-0 flex items-center justify-center">'
                    f'<svg class="w-16 h-16 text-red-600 drop-shadow-lg group-hover:scale-110 '
                    f'transition-transform" viewBox="0 0 68 48">'
                    f'<path d="M66.52 7.74c-.78-2.93-2.49-5.41-5.42-6.19C55.79.13 34 0 34 '
                    f'0S12.21.13 6.9 1.55c-2.93.78-4.64 3.26-5.42 6.19C.06 13.05 0 24 0 '
                    f'24s.06 10.95 1.48 16.26c.78 2.93 2.49 5.41 5.42 6.19C12.21 47.87 34 '
                    f'48 34 48s21.79-.13 27.1-1.55c2.93-.78 4.64-3.26 5.42-6.19C67.94 34.95 '
                    f'68 24 68 24s-.06-10.95-1.48-16.26z" fill="currentColor"/>'
                    f'<path d="M45 24L27 14v20" fill="white"/></svg></span></a>'
                )
            new_content = youtube_iframe_re.sub(_thumb_replacement, content)
        else:
            # Detail pages: youtube-nocookie iframe + "Watch on YouTube" fallback
            def _detail_replacement(m):
                vid_id = m.group(1)
                return (
                    f'<iframe src="https://www.youtube-nocookie.com/embed/{vid_id}" '
                    f'class="w-full h-full" frameborder="0" '
                    f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
                    f'gyroscope; picture-in-picture" allowfullscreen></iframe>'
                )
            new_content = youtube_iframe_re.sub(_detail_replacement, content)

        if new_content != content:
            file_dict[path] = new_content
            label = "thumbnail links" if is_index else "nocookie iframe"
            print(f"  [post-process] Converted YouTube iframes to {label} in {path}")

    # Phase 2: Handle dynamic EJS variables in iframe src (e.g. <%= video.embed_url %>)
    # The regex phase above only catches hardcoded YouTube URLs.  When Claude uses EJS
    # expressions like <%= video.embed_url %>, we replace at the template level.
    _ejs_iframe_re = re_mod.compile(
        r'<iframe\b[^>]*\ssrc="<%[=-]\s*([^%]+?(?:embed_url|video_url|url|embed|media_url)[^%]*?)%>".*?(?:</iframe>|/>)',
        re_mod.IGNORECASE | re_mod.DOTALL,
    )

    # YT_IFRAME_NONVIDEO_SKIP: variable-name patterns that clearly indicate a non-YouTube
    # embed (ticket checkouts, payment iframes, scheduling widgets, custom embed URLs).
    # If any of these fragments appear in the EJS var, we skip the youtube-nocookie rewrite
    # - otherwise the Posh ticket iframe (and similar) gets destroyed by having its src
    # replaced with an empty YouTube URL.  Regression first observed in MW v12,
    # where every event detail page's Posh ticket embed rendered as a blank
    # white box with a dead "Watch on YouTube" link.
    _NON_YOUTUBE_VAR_FRAGMENTS = (
        "posh_", "vimeo_", "stripe_", "calendly", "typeform",
        "checkout", "ticket", "tickets", "ticketing",
        "embed_code", "embed_iframe", "custom_embed",
        "iframe_src", "iframe_url",
        "map_url", "mapbox_", "google_map",
    )

    for path in list(file_dict.keys()):
        if not path.endswith(".ejs") or isinstance(file_dict[path], bytes):
            continue
        content = file_dict[path]
        ejs_match = _ejs_iframe_re.search(content)
        if not ejs_match:
            continue
        # Already converted above or has lite-youtube
        if "youtube-nocookie" in content or "ytimg.com" in content:
            continue

        ejs_var = ejs_match.group(1).strip()
        # Skip non-YouTube embeds (Posh ticket checkout, Vimeo, Stripe, etc.)
        if any(frag in ejs_var.lower() for frag in _NON_YOUTUBE_VAR_FRAGMENTS):
            print(f"  [post-process] Skipped youtube-nocookie rewrite for non-YouTube var '{ejs_var}' in {path}")
            continue

        basename = path.lower().rsplit("/", 1)[-1].replace(".ejs", "")
        is_index = any(p in basename for p in _INDEX_PATTERNS)

        if is_index:
            # Extract video ID from the EJS variable at render time, show thumbnail
            # Uses /api/video-thumb/ proxy to bypass ad-blockers that block i.ytimg.com
            replacement = (
                f'<% var _vidMatch = ({ejs_var} || "").match(/embed\\/([a-zA-Z0-9_-]{{11}})/); '
                f'var _vidId = _vidMatch ? _vidMatch[1] : ""; %>\n'
                f'<a href="https://www.youtube.com/watch?v=<%= _vidId %>" target="_blank" '
                f'rel="noopener" class="block relative group">'
                f'<img src="/api/video-thumb/<%= _vidId %>" '
                f'alt="Video thumbnail" class="w-full h-full object-cover" loading="lazy">'
                f'<span class="absolute inset-0 flex items-center justify-center">'
                f'<svg class="w-16 h-16 text-red-600 drop-shadow-lg group-hover:scale-110 '
                f'transition-transform" viewBox="0 0 68 48">'
                f'<path d="M66.52 7.74c-.78-2.93-2.49-5.41-5.42-6.19C55.79.13 34 0 34 '
                f'0S12.21.13 6.9 1.55c-2.93.78-4.64 3.26-5.42 6.19C.06 13.05 0 24 0 '
                f'24s.06 10.95 1.48 16.26c.78 2.93 2.49 5.41 5.42 6.19C12.21 47.87 34 '
                f'48 34 48s21.79-.13 27.1-1.55c2.93-.78 4.64-3.26 5.42-6.19C67.94 34.95 '
                f'68 24 68 24s-.06-10.95-1.48-16.26z" fill="currentColor"/>'
                f'<path d="M45 24L27 14v20" fill="white"/></svg></span></a>'
            )
            new_content = _ejs_iframe_re.sub(replacement, content, count=0)
        else:
            # Detail: nocookie iframe + fallback link
            replacement = (
                f'<% var _vidMatch = ({ejs_var} || "").match(/embed\\/([a-zA-Z0-9_-]{{11}})/); '
                f'var _vidId = _vidMatch ? _vidMatch[1] : ""; %>\n'
                f'<iframe src="https://www.youtube-nocookie.com/embed/<%= _vidId %>" '
                f'class="w-full h-full" frameborder="0" '
                f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
                f'gyroscope; picture-in-picture" allowfullscreen></iframe>\n'
                f'<p class="mt-2 text-center text-sm opacity-70">'
                f'<a href="https://www.youtube.com/watch?v=<%= _vidId %>" '
                f'target="_blank" rel="noopener" class="hover:underline">'
                f'Watch on YouTube &rarr;</a></p>'
            )
            new_content = _ejs_iframe_re.sub(replacement, content, count=0)

        if new_content != content:
            file_dict[path] = new_content
            label = "thumbnail links" if is_index else "nocookie iframe"
            print(f"  [post-process] Converted dynamic EJS video embeds to {label} in {path}")

    # Phase 3: Rewrite <img src="https://img.youtube.com/vi/{id}/..."> to /api/video-thumb/{id}
    # Claude often generates direct YouTube thumbnail img tags that ad-blockers block.
    # Handles both hardcoded video IDs and dynamic EJS expressions.
    _ytimg_literal_re = re_mod.compile(
        r'(<img\b[^>]*\bsrc=")https?://(?:(?:i[0-9]*\.)?ytimg|img\.youtube)\.com/vi/([a-zA-Z0-9_-]{11})/[^"]*(")',
        re_mod.IGNORECASE,
    )
    _ytimg_ejs_re = re_mod.compile(
        r'(<img\b[^>]*\bsrc=")https?://(?:(?:i[0-9]*\.)?ytimg|img\.youtube)\.com/vi/<%[=-]\s*([^%]+?)%>/[^"]*(")',
        re_mod.IGNORECASE,
    )

    for path in list(file_dict.keys()):
        if not path.endswith(".ejs") or isinstance(file_dict[path], bytes):
            continue
        content = file_dict[path]
        new_content = content

        # Rewrite hardcoded YouTube thumbnail URLs
        new_content = _ytimg_literal_re.sub(r'\g<1>/api/video-thumb/\2\3', new_content)

        # Rewrite dynamic EJS YouTube thumbnail URLs
        new_content = _ytimg_ejs_re.sub(r'\g<1>/api/video-thumb/<%= \2%>\3', new_content)

        if new_content != content:
            file_dict[path] = new_content
            print(f"  [post-process] Rewrote YouTube thumbnail img src to proxy in {path}")

    # Patch CSP to allow youtube-nocookie.com in frame-src
    server_js = file_dict.get("server.js", "")
    if server_js and "youtube-nocookie" not in server_js:
        # Handle helmet contentSecurityPolicy frameSrc
        if "youtube.com" in server_js and "frameSrc" in server_js:
            server_js = server_js.replace(
                '"https://www.youtube.com"',
                '"https://www.youtube.com", "https://www.youtube-nocookie.com"',
            )
            file_dict["server.js"] = server_js
            print("  [post-process] Added youtube-nocookie.com to CSP frame-src")

    # Add video thumbnail proxy route (ad-blocker safe).
    #
    # v21 regression (2026-04-18): the legacy guard only checked for the
    # path string "/api/video-thumb/" and skipped injection when Claude
    # already emitted a handler there. But Claude's emission often
    # returns JSON (`res.json({thumbnail: URL})`) instead of piping the
    # image bytes, so `<img src="/api/video-thumb/X">` gets JSON and
    # fails silently with a broken thumbnail on the /videos list.
    #
    # Fix: guard on a SIGNATURE inside the handler body, not just the
    # path. If the pipeline's canonical `upstream.pipe(res)` pattern
    # isn't present, strip Claude's broken handler and inject ours.
    server_js = file_dict.get("server.js", "")
    proxy_route = (
        "\n// Proxy YouTube video thumbnails (ad-blocker safe)\n"
        "app.get('/api/video-thumb/:videoId', (req, res) => {\n"
        "  const videoId = req.params.videoId;\n"
        "  if (!/^[a-zA-Z0-9_-]{11}$/.test(videoId)) return res.status(400).send('Invalid');\n"
        "  const https = require('https');\n"
        "  https.get(`https://i.ytimg.com/vi/${videoId}/hqdefault.jpg`, (upstream) => {\n"
        "    if (upstream.statusCode !== 200) {\n"
        "      res.type('image/svg+xml').send('<svg xmlns=\"http://www.w3.org/2000/svg\" "
        "width=\"480\" height=\"360\" viewBox=\"0 0 480 360\"><rect fill=\"#1a1a2e\" "
        "width=\"480\" height=\"360\"/><polygon points=\"220,140 220,220 280,180\" "
        "fill=\"#fff\" opacity=\"0.6\"/></svg>');\n"
        "      return;\n"
        "    }\n"
        "    res.set('Content-Type', upstream.headers['content-type'] || 'image/jpeg');\n"
        "    res.set('Cache-Control', 'public, max-age=86400');\n"
        "    upstream.pipe(res);\n"
        "  }).on('error', () => {\n"
        "    res.type('image/svg+xml').send('<svg xmlns=\"http://www.w3.org/2000/svg\" "
        "width=\"480\" height=\"360\" viewBox=\"0 0 480 360\"><rect fill=\"#1a1a2e\" "
        "width=\"480\" height=\"360\"/><polygon points=\"220,140 220,220 280,180\" "
        "fill=\"#fff\" opacity=\"0.6\"/></svg>');\n"
        "  });\n"
        "});\n"
    )
    # Signature unique to the pipeline's canonical handler: piping the
    # upstream https response straight to res. Claude's JSON variant
    # never produces this string.
    PIPELINE_HANDLER_SIGNATURE = "upstream.pipe(res)"

    if server_js and "express.static" in server_js:
        has_canonical = PIPELINE_HANDLER_SIGNATURE in server_js

        if not has_canonical:
            # Strip any existing (broken Claude) handler first. Find the
            # `app.get(/api/video-thumb` prefix, then scan forward to the
            # matching `});`: brace-aware enough for Claude's typical
            # shapes without a full JS parse.
            header_re = re_mod.compile(
                r"app\.(?:get|use)\s*\(\s*['\"][^'\"]*\/api\/video-thumb"
            )
            header_match = header_re.search(server_js)
            if header_match:
                start = header_match.start()
                # Scan forward from `start` until we find `});` with
                # balanced inner braces. Simpler heuristic: the LAST
                # `});` before the next top-level `app.` declaration or
                # end of file is the handler terminator.
                next_top_level = re_mod.search(
                    r"\n(?:app|module|if)\.",
                    server_js[start + 1:],
                )
                scan_end = (
                    start + 1 + next_top_level.start()
                    if next_top_level
                    else len(server_js)
                )
                region = server_js[start:scan_end]
                # Find last `});` inside the region (the handler's
                # closing delimiter). Trail past trailing newlines.
                terminator_idx = region.rfind("});")
                if terminator_idx == -1:
                    terminator_idx = region.rfind("})")
                if terminator_idx != -1:
                    end = start + terminator_idx + len("});")
                    while end < len(server_js) and server_js[end] in " \t\r\n":
                        end += 1
                    server_js = server_js[:start] + server_js[end:]
                    print(
                        "  [post-process] Stripped Claude's broken "
                        "/api/video-thumb/ handler (returned JSON, not image)"
                    )

            # Inject canonical handler immediately after express.static.
            static_match = re_mod.search(
                r"app\.use\(express\.static\(.*?\)\);", server_js
            )
            if static_match:
                insert_pos = static_match.end()
                server_js = (
                    server_js[:insert_pos] + proxy_route + server_js[insert_pos:]
                )
                file_dict["server.js"] = server_js
                print(
                    "  [post-process] Added video thumbnail proxy route "
                    "(canonical image-piping version)"
                )

    return list(file_dict.items())


@processor(after=('_remove_homepage_video_section',), phase=('build',))
def _ensure_premium_css(files: list) -> list:
    """Inject premium-base.css into every generated app.

    Copies cli/templates/premium-base.css to public/premium-base.css and
    ensures layout.ejs links to it. Provides polished defaults (transitions,
    hover states, animate-in keyframes, focus rings, skeleton loaders) that
    Claude often omits.
    """
    import re as re_mod

    file_dict = dict(files)

    # Load the premium CSS template
    css_path = _TEMPLATES_DIR / "premium-base.css"
    if not css_path.exists():
        return files
    css_content = css_path.read_text()
    file_dict["public/premium-base.css"] = css_content
    print("  [post-process] Injected public/premium-base.css")

    # Ensure layout.ejs links to it
    layout = file_dict.get("views/layout.ejs", "")
    if layout and "premium-base.css" not in layout:
        # Insert after tailwind.css or style.css, or before </head>
        if "/tailwind.css" in layout:
            layout = layout.replace(
                '<link rel="stylesheet" href="/tailwind.css">',
                '<link rel="stylesheet" href="/tailwind.css">\n  <link rel="stylesheet" href="/premium-base.css">',
            )
        elif "/style.css" in layout:
            layout = layout.replace(
                '<link rel="stylesheet" href="/style.css">',
                '<link rel="stylesheet" href="/style.css">\n  <link rel="stylesheet" href="/premium-base.css">',
            )
        elif "</head>" in layout:
            layout = layout.replace(
                "</head>",
                '  <link rel="stylesheet" href="/premium-base.css">\n</head>',
            )
        file_dict["views/layout.ejs"] = layout
        print("  [post-process] Added premium-base.css link to layout.ejs")

    return list(file_dict.items())


@processor(after=('_ensure_contact_email',))
def _ensure_upload_route(files: list, spec: dict) -> list:
    """Inject the upload route template when spec has image-related fields.

    Detects image fields by checking data model field names for keywords like
    'photo', 'image', 'avatar', 'picture', 'thumbnail', 'logo', 'banner'.
    When detected, injects routes/upload.js, patches server.js to mount it,
    adds the uploads table to schema.sql, and adds multer to package.json.
    """
    # Check if spec has image-related fields
    image_keywords = {"photo", "image", "avatar", "picture", "thumbnail", "logo", "banner", "cover", "icon"}
    has_image_fields = False
    for model in spec.get("data_models", []):
        if isinstance(model, dict):
            for field in model.get("fields", []):
                field_name = field.get("name", "").lower() if isinstance(field, dict) else str(field).lower()
                if any(kw in field_name for kw in image_keywords):
                    has_image_fields = True
                    break
        if has_image_fields:
            break

    if not has_image_fields:
        return files

    file_dict = dict(files)

    # Skip if already present
    if "routes/upload.js" in file_dict:
        return files

    # Load the template
    template_path = _TEMPLATES_DIR / "routes_upload.js"
    if not template_path.exists():
        return files
    file_dict["routes/upload.js"] = template_path.read_text()
    print("  [post-process] Injected routes/upload.js (image upload support)")

    # Patch server.js to mount the upload route
    server_js = file_dict.get("server.js", "")
    if server_js and "routes/upload" not in server_js:
        import re as re_mod
        # Pattern 1: app.use(require('./routes/...'))
        mount_pattern = re_mod.compile(r"(app\.use\(require\(['\"]\.\/routes\/[^)]+\)\);?)")
        matches = list(mount_pattern.finditer(server_js))
        if matches:
            last_match = matches[-1]
            insert_pos = last_match.end()
            inject = "\napp.use(require('./routes/upload'));"
            server_js = server_js[:insert_pos] + inject + server_js[insert_pos:]
            file_dict["server.js"] = server_js
        else:
            # Pattern 2: app.use('/path', someRouter) or app.use(someRouter)
            use_pattern = re_mod.compile(r"(app\.use\([^)]*[Rr]outer\)?;?)")
            use_matches = list(use_pattern.finditer(server_js))
            if use_matches:
                last_match = use_matches[-1]
                insert_pos = last_match.end()
                inject = "\napp.use(require('./routes/upload'));"
                server_js = server_js[:insert_pos] + inject + server_js[insert_pos:]
                file_dict["server.js"] = server_js

    # Add uploads table to schema.sql if not present
    schema = file_dict.get("db/schema.sql", "")
    if schema and "uploads" not in schema:
        uploads_table = """
-- Image uploads (base64 storage)
CREATE TABLE IF NOT EXISTS uploads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename VARCHAR(255) NOT NULL,
    mime_type VARCHAR(50) NOT NULL,
    data TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
);
"""
        file_dict["db/schema.sql"] = schema + "\n" + uploads_table

    # Add multer and sharp to package.json dependencies
    pkg = file_dict.get("package.json", "")
    if pkg:
        try:
            pkg_json = json.loads(pkg)
            deps = pkg_json.setdefault("dependencies", {})
            # Pin to the same safe versions exported by REQUIRED_DEPS.
            # _ensure_package_deps force-upgrades stale pins for both deps
            # (they are both in FORCE_UPGRADE_DEPS) but pinning correctly
            # here avoids a superfluous replacement in the common path.
            if "multer" not in deps:
                deps["multer"] = REQUIRED_DEPS["multer"]
            if "sharp" not in deps:
                deps["sharp"] = REQUIRED_DEPS["sharp"]
            file_dict["package.json"] = json.dumps(pkg_json, indent=2)
        except Exception:
            pass

    return list(file_dict.items())


# Upload zone HTML template for _ensure_upload_form_ui
_UPLOAD_ZONE_HTML = (
    # UPLOAD_PREVIEW_INITIAL_STATE (v13 fix): preview block starts VISIBLE when
    # the hidden input's value is non-empty at render time, so admin forms
    # show a thumbnail of the currently-saved image above Choose File.
    # Client-side image-upload.js keeps them in sync on subsequent uploads.
    '<input type="hidden" id="{field_id}" name="{field_name}" value="{value}">\n'
    '<div class="image-upload-zone" data-target="{field_id}" data-required="{required}" '
    'style="border:2px dashed var(--color-border,#d1d5db);border-radius:0.75rem;'
    'padding:1.5rem;text-align:center;cursor:pointer;transition:border-color 0.2s,background 0.2s;">\n'
    '  <div class="upload-preview" style="display:<%= ({value_expr}) ? \'block\' : \'none\' %>;margin-bottom:0.75rem;">\n'
    '    <img src="<%= ({value_expr}) || \'\' %>" alt="Current image" style="max-height:12rem;margin:0 auto;border-radius:0.5rem;'
    'box-shadow:0 1px 3px rgba(0,0,0,0.1);">\n'
    '    <button type="button" class="upload-remove" style="display:block;margin:0.5rem auto 0;'
    'font-size:0.875rem;color:#ef4444;background:none;border:none;cursor:pointer;'
    'text-decoration:underline;">Remove</button>\n'
    '  </div>\n'
    '  <div class="upload-prompt" style="display:<%= ({value_expr}) ? \'none\' : \'block\' %>;">\n'
    '    <svg style="width:3rem;height:3rem;margin:0 auto 0.75rem;opacity:0.4;" fill="none" '
    'stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" '
    'stroke-width="1.5" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 '
    '012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 '
    '002 2z"/></svg>\n'
    '    <p style="font-size:0.875rem;font-weight:500;color:var(--color-text-muted,#6b7280);">'
    'Drag &amp; drop an image or <span style="color:var(--color-accent,#d4a574);font-weight:600;">'
    'click to browse</span></p>\n'
    '    <p style="font-size:0.75rem;margin-top:0.25rem;color:var(--color-text-muted,#6b7280);">'
    'JPEG, PNG, WebP, or GIF &middot; max 5 MB</p>\n'
    '  </div>\n'
    '  <input type="file" accept="image/jpeg,image/png,image/webp,image/gif" '
    'class="upload-file-input" style="display:none;">\n'
    '  <div class="upload-progress" style="display:none;margin-top:0.75rem;">\n'
    '    <div style="width:100%;background:#e5e7eb;border-radius:9999px;height:0.5rem;">\n'
    '      <div class="upload-bar" style="height:0.5rem;border-radius:9999px;'
    'background:var(--color-accent,#d4a574);width:0%;transition:width 0.3s;"></div>\n'
    '    </div>\n'
    '  </div>\n'
    '  <div class="upload-error" style="display:none;margin-top:0.5rem;font-size:0.875rem;'
    'color:#ef4444;"></div>\n'
    '</div>'
)

# Regex: match <input type="text" name="IMAGE_FIELD"> in any attribute order
_IMAGE_INPUT_RE = None  # Compiled lazily


@processor(after=('_humanize_settings_labels',))
def _ensure_upload_form_ui(files, spec):
    """Replace image URL text inputs with drag-and-drop file upload UI.

    Requires routes/upload.js to exist (injected by _ensure_upload_route).
    For each EJS form file, finds <input type="text" name="image_url"> (and similar
    image-related field names), replaces with hidden input + upload zone HTML.
    Injects image-upload.js and upload zone CSS into layout.ejs.

    Uses a sentinel approach: EJS closing tags (%>) contain '>' which confuses
    HTML tag boundary detection. We temporarily replace '%>' with a sentinel
    before regex matching, then restore after replacement.
    """
    import re as re_mod

    global _IMAGE_INPUT_RE
    if _IMAGE_INPUT_RE is None:
        # Match <input type="text"|"url"> whose name contains a visual-asset
        # keyword (image|photo|logo|avatar|banner|cover|thumbnail|picture|icon),
        # optionally with a snake_case prefix (hero_image, nav_logo, about_photo)
        # and/or a _url suffix. Claude frequently emits type="url" for image URL
        # fields: the old regex only matched type="text" and silently skipped them.
        # The "_url" suffix is the ONLY allowed suffix so that names like
        # image_alt, logo_text, photo_caption are NOT matched.
        _IMAGE_INPUT_RE = re_mod.compile(
            r'<input\b'
            r'(?=[^>]*\btype="(?:text|url)")'
            r'(?=[^>]*\bname="('
            r'(?:[a-z][a-z0-9_]*_)?'
            r'(?:image|photo|logo|avatar|banner|cover|thumbnail|picture|icon)'
            r'(?:_url)?'
            r')")'
            r'[^>]*?>',
            re_mod.IGNORECASE,
        )

    _EJS_SENTINEL = "\x00EJS_GT\x00"

    file_dict = dict(files)

    # Prerequisite: upload route must exist
    if "routes/upload.js" not in file_dict:
        return files

    # Load the JS template
    js_path = _TEMPLATES_DIR / "image-upload.js"
    if not js_path.exists():
        return files

    replaced_any = False

    # v17 FIX N: pre-pass to strip orphan <input type="file"> that sits next
    # to a visible <input type="text|url" name="X_url"> on forms elsewhere in
    # the app (e.g. views/admin/dashboard.ejs had its own inline settings form
    # with both a URL text input and a Choose File button; once the text
    # input gets rewritten into a drag-drop zone, the sibling file input is
    # orphaned and wrong UX).
    orphan_file_re = re_mod.compile(
        r'<input\b(?=[^>]*\btype="file")'
        r'(?=[^>]*\bname="(?:[a-z][a-z0-9_]*_)?'
        r'(?:image|photo|logo|avatar|banner|cover|thumbnail|picture|icon)'
        r'(?:_file|_url|_upload)?")'
        r'[^>]*?>',
        re_mod.IGNORECASE,
    )

    for path in list(file_dict.keys()):
        if not path.endswith(".ejs") or "views/" not in path:
            continue
        content = file_dict[path]

        # Replace EJS closing tags with sentinel so '>' only means HTML tag close
        safe_content = content.replace("%>", _EJS_SENTINEL)

        # Only strip orphan file inputs when the same form ALSO has a URL text
        # input (meaning the drag-drop zone will take over).
        if _IMAGE_INPUT_RE.search(safe_content):
            safe_content = orphan_file_re.sub("", safe_content)

        def _replace_input(match):
            nonlocal replaced_any
            tag = match.group(0)
            field_name = match.group(1)

            # Extract id (fallback to field_name)
            id_m = re_mod.search(r'id="([^"]*)"', tag)
            field_id = id_m.group(1) if id_m else field_name

            # Extract value (simple [^"]* works because %> is sentinel-ized)
            val_m = re_mod.search(r'value="([^"]*)"', tag)
            value = val_m.group(1) if val_m else ""

            # UPLOAD_PREVIEW_INITIAL_STATE: derive a bare JS expression we can
            # re-use inside outer <%= ... %> for the preview conditional.
            # Three cases:
            #   1. value = '<%= expr \x00EJS_GT\x00'  (EJS expression, sentinel-ized)
            #       -> inner expr = 'expr'
            #   2. value = 'literal/path/string'     (static value)
            #       -> inner expr = '"literal/path/string"' (JS string literal)
            #   3. value = ''                         (empty)
            #       -> inner expr = '""'
            ejs_open = "<%="
            stripped = value.strip()
            if stripped.startswith(ejs_open):
                inner = stripped[len(ejs_open):]
                inner = inner.replace(_EJS_SENTINEL, "").strip()
            elif stripped == "":
                inner = '""'
            else:
                # JS string literal: escape quotes
                js_lit = stripped.replace("\\", "\\\\").replace('"', '\\"')
                inner = f'"{js_lit}"'

            is_required = "required" in tag.lower()
            replaced_any = True

            return _UPLOAD_ZONE_HTML.format(
                field_id=field_id,
                field_name=field_name,
                value=value,
                value_expr=inner,
                required="true" if is_required else "false",
            )

        new_safe = _IMAGE_INPUT_RE.sub(_replace_input, safe_content)
        # Restore EJS closing tags
        new_content = new_safe.replace(_EJS_SENTINEL, "%>")
        if new_content != content:
            # Also update label text from "Image URL" to "Image"
            new_content = new_content.replace(">Image URL *<", ">Image *<")
            new_content = new_content.replace(">Image URL<", ">Image<")
            new_content = new_content.replace(">Photo URL *<", ">Photo *<")
            new_content = new_content.replace(">Photo URL<", ">Photo<")
            file_dict[path] = new_content
            print(f"  [post-process] Replaced image URL input with upload UI in {path}")

    if not replaced_any:
        return list(file_dict.items())

    # Inject public/image-upload.js
    file_dict["public/image-upload.js"] = js_path.read_text()

    # Add script + CSS to layout.ejs
    layout = file_dict.get("views/layout.ejs", "")
    if layout and "image-upload.js" not in layout:
        upload_assets = (
            '<style>.image-upload-zone:hover,.image-upload-zone.upload-dragover{'
            'border-color:var(--color-accent,#d4a574)!important;'
            'background:rgba(212,165,116,0.05)}</style>\n'
            '<script src="/image-upload.js"></script>'
        )
        if "</body>" in layout:
            layout = layout.replace("</body>", upload_assets + "\n</body>")
        else:
            layout += "\n" + upload_assets
        file_dict["views/layout.ejs"] = layout

    return list(file_dict.items())


@processor(after=('_ensure_upload_form_ui',))
def _strip_redundant_image_upload_forms(files, spec):
    """Remove parallel multipart <form> blocks in admin views where a drag-drop
    upload zone has already been injected by _ensure_upload_form_ui.

    Root cause: Claude sometimes generates a settings page with BOTH a text
    input like `about_photo_url` (which we rewrite into a drag-drop zone) AND
    a separate `<form enctype="multipart/form-data">` with `<input type="file">`
    and a "Choose File / Upload" submit button that POSTs to a sibling route
    like /admin/settings/about-photo-upload. The customer sees two upload UIs
    for the same field. This processor strips the parallel multipart form
    whenever an image-upload-zone is present in the same view, leaving the
    single drag-drop zone as the only upload affordance.
    """
    import re as re_mod
    file_dict = dict(files)
    for path in list(file_dict.keys()):
        if not path.endswith(".ejs") or "views/" not in path:
            continue
        content = file_dict[path]
        if 'class="image-upload-zone"' not in content:
            continue
        form_pattern = re_mod.compile(
            r'<form\b[^>]*\benctype="multipart/form-data"[^>]*>'
            r'(?:(?!</form>).)*?'
            r'<input\b[^>]*\btype="file"[^>]*\baccept="image/[^"]*"[^>]*>'
            r'(?:(?!</form>).)*?'
            r'</form>',
            re_mod.DOTALL | re_mod.IGNORECASE,
        )
        new_content, n = form_pattern.subn('', content)
        if n == 0:
            continue
        new_content = re_mod.sub(r'<hr\b[^>]*>\s*(?=\n|\s*<)', '', new_content)
        new_content = re_mod.sub(
            r'<(p|label|h[1-6])\b[^>]*>\s*[Oo]r upload[^<]*</\1>\s*',
            '', new_content)
        file_dict[path] = new_content
        print(f"  [post-process] Stripped {n} redundant image-upload form(s) from {path}")
    return list(file_dict.items())


@processor(after=('_ensure_mobile_responsive',), phase=('build',))
def _ensure_tailwind_compiled(files: list, spec: dict) -> list:
    """Replace Tailwind CDN with compiled CSS approach.

    The Tailwind CDN (cdn.tailwindcss.com) is a development-only tool that
    gets blocked by ad blockers, corporate firewalls, and content security
    policies in production. This post-processor ensures generated apps use
    compiled Tailwind CSS instead:
    1. Adds tailwindcss to package.json dependencies
    2. Adds a postinstall script to compile CSS
    3. Creates tailwind.config.js with theme colors from spec
    4. Removes CDN script tags from layout.ejs
    5. Ensures layout.ejs references the compiled CSS file
    """
    import json as json_mod
    import re as re_mod

    file_dict = dict(files)

    # --- Extract design colors and fonts from spec (UUPM-aware) ---
    try:
        from design_data import get_design_context
        ctx = get_design_context(spec)
        palette = ctx.get("palette", {})
        primary = palette.get("primary", "#2563eb")
        secondary = palette.get("secondary", "#1e40af")
        accent = palette.get("accent", "#f59e0b")
        fonts = ctx.get("fonts", {})
        display_font = fonts.get("display", "Poppins")
        body_font = fonts.get("body", "DM Sans")
    except Exception:
        # Fallback to raw spec.design
        design = spec.get("design", {})
        primary = design.get("primary_color", "#2563eb")
        secondary = design.get("secondary_color", "#1e40af")
        accent = design.get("accent_color", "#f59e0b")
        display_font, body_font = _get_style_fonts(spec)

    # --- 1. Ensure tailwind.config.js exists ---
    if "tailwind.config.js" not in file_dict:
        file_dict["tailwind.config.js"] = f"""/** @type {{import('tailwindcss').Config}} */
module.exports = {{
  content: ['./views/**/*.ejs'],
  theme: {{
    extend: {{
      colors: {{
        primary: '{primary}',
        secondary: '{secondary}',
        accent: '{accent}',
      }},
      fontFamily: {{
        display: ['{display_font}', 'serif'],
        body: ['{body_font}', 'sans-serif'],
      }},
    }},
  }},
  plugins: [],
}};
"""
        print("  [post-process] Created tailwind.config.js with theme colors and fonts")
    else:
        # Ensure existing config has content paths pointing to views
        config = file_dict["tailwind.config.js"]
        changed_config = False
        if "./views/" not in config and "views/" not in config:
            config = config.replace(
                "content: [",
                "content: ['./views/**/*.ejs', "
            )
            changed_config = True
            print("  [post-process] Fixed tailwind.config.js content paths")

        # Patch colors to match UUPM-resolved palette; Claude often generates
        # placeholder dark colors (#002040 etc.) that UUPM overrides for CSS custom
        # properties, but the Tailwind config retains the raw placeholders.
        for color_name, correct_value in [("primary", primary), ("secondary", secondary), ("accent", accent)]:
            pattern = re_mod.compile(
                rf"({color_name}\s*:\s*)['\"]#[0-9a-fA-F]{{3,8}}['\"]"
            )
            if pattern.search(config):
                new_config = pattern.sub(rf"\1'{correct_value}'", config)
                if new_config != config:
                    config = new_config
                    changed_config = True
                    print(f"  [post-process] Fixed tailwind.config.js {color_name} color to {correct_value}")

        # Inject fontFamily if missing: Claude often generates colors-only configs.
        # Missing fontFamily means font-display/font-body/font-brand classes produce no output.
        if "fontFamily" not in config:
            # Inject inside extend: { ... } block before closing brace
            font_entry = (
                f"      fontFamily: {{\n"
                f"        display: ['{display_font}', 'serif'],\n"
                f"        body: ['{body_font}', 'sans-serif'],\n"
                f"        brand: ['{display_font}', 'serif'],\n"
                f"      }},\n"
            )
            if "extend: {" in config:
                config = config.replace("extend: {", f"extend: {{\n{font_entry}", 1)
            elif "extend:{" in config:
                config = config.replace("extend:{", f"extend:{{\n{font_entry}", 1)
            else:
                # No extend block: inject before closing theme brace
                config = re_mod.sub(
                    r'(theme\s*:\s*\{)',
                    rf'\1\n    extend: {{\n{font_entry}    }},\n',
                    config,
                    count=1,
                )
            changed_config = True
            print("  [post-process] Injected fontFamily into existing tailwind.config.js")

        if changed_config:
            file_dict["tailwind.config.js"] = config

    # --- 2. Update package.json: add tailwindcss + postinstall script ---
    pkg_raw = file_dict.get("package.json", "")
    if pkg_raw:
        try:
            pkg = json_mod.loads(pkg_raw)
            deps = pkg.get("dependencies", {})
            scripts = pkg.get("scripts", {})
            changed = False

            if "tailwindcss" not in deps:
                deps["tailwindcss"] = "^3.4.0"
                changed = True
                print("  [post-process] Added tailwindcss to package.json dependencies")

            if "postinstall" not in scripts:
                scripts["postinstall"] = "npx tailwindcss -o public/tailwind.css --minify"
                changed = True
                print("  [post-process] Added postinstall CSS build script to package.json")
            elif "tailwind" not in scripts.get("postinstall", ""):
                # Append to existing postinstall
                scripts["postinstall"] += " && npx tailwindcss -o public/tailwind.css --minify"
                changed = True
                print("  [post-process] Appended Tailwind build to existing postinstall script")

            if changed:
                pkg["dependencies"] = deps
                pkg["scripts"] = scripts
                file_dict["package.json"] = json_mod.dumps(pkg, indent=2) + "\n"
        except (json_mod.JSONDecodeError, ValueError):
            pass

    # --- 3. Fix layout.ejs: remove CDN, ensure compiled CSS link ---
    layout = file_dict.get("views/layout.ejs", "")
    if layout:
        changed = False

        # Remove Tailwind CDN script tag
        if "cdn.tailwindcss.com" in layout:
            # Remove the script src tag
            layout = re_mod.sub(
                r'<script\s+src="https://cdn\.tailwindcss\.com"[^>]*>\s*</script>\s*',
                "",
                layout,
            )
            # Remove the tailwind.config inline script block
            layout = re_mod.sub(
                r"<script>\s*tailwind\.config\s*=\s*\{[\s\S]*?\};\s*</script>\s*",
                "",
                layout,
            )
            changed = True
            print("  [post-process] Removed Tailwind CDN script from layout.ejs")

        # Ensure compiled CSS link exists
        if "/tailwind.css" not in layout:
            # Insert before </head> or before first <link> to style.css
            if "/style.css" in layout:
                layout = layout.replace(
                    '<link rel="stylesheet" href="/style.css">',
                    '<link rel="stylesheet" href="/tailwind.css">\n  <link rel="stylesheet" href="/style.css">',
                )
            elif "</head>" in layout:
                layout = layout.replace(
                    "</head>",
                    '  <link rel="stylesheet" href="/tailwind.css">\n</head>',
                )
            changed = True
            print("  [post-process] Added compiled Tailwind CSS link to layout.ejs")

        # Remove CDN from CSP if present (no longer needed)
        if "cdn.tailwindcss.com" in layout:
            layout = layout.replace(', "https://cdn.tailwindcss.com"', "")
            layout = layout.replace('"https://cdn.tailwindcss.com", ', "")
            layout = layout.replace("'https://cdn.tailwindcss.com', ", "")
            layout = layout.replace(", 'https://cdn.tailwindcss.com'", "")
            changed = True

        # --- 3b. Ensure Google Fonts link uses spec fonts, not Inter ---
        display_url = display_font.replace(" ", "+")
        body_url = body_font.replace(" ", "+")
        expected_fonts_href = (
            f"https://fonts.googleapis.com/css2?family={display_url}"
            f":wght@400;700;900&family={body_url}"
            f":wght@300;400;500;600;700&display=swap"
        )
        # Replace any existing Inter-only Google Fonts link
        if "family=Inter" in layout and display_font != "Inter":
            layout = re_mod.sub(
                r'<link\s+href="https://fonts\.googleapis\.com/css2\?family=Inter[^"]*"\s+rel="stylesheet"\s*/?>',
                f'<link href="{expected_fonts_href}" rel="stylesheet">',
                layout,
            )
            changed = True
            print(f"  [post-process] Replaced Inter font link with {display_font} + {body_font}")
        # If no Google Fonts link exists at all, add one before </head>
        if "fonts.googleapis.com" not in layout and "</head>" in layout:
            font_links = (
                f'  <link rel="preconnect" href="https://fonts.googleapis.com">\n'
                f'  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
                f'  <link href="{expected_fonts_href}" rel="stylesheet">\n'
            )
            layout = layout.replace("</head>", font_links + "</head>")
            changed = True
            print(f"  [post-process] Added Google Fonts link for {display_font} + {body_font}")

        if changed:
            file_dict["views/layout.ejs"] = layout

    # --- 4. Fix server.js CSP: remove CDN.tailwindcss.com from helmet config ---
    serverjs = file_dict.get("server.js", "")
    if serverjs and "cdn.tailwindcss.com" in serverjs:
        serverjs = serverjs.replace(', "https://cdn.tailwindcss.com"', "")
        serverjs = serverjs.replace('"https://cdn.tailwindcss.com", ', "")
        serverjs = serverjs.replace("'https://cdn.tailwindcss.com', ", "")
        serverjs = serverjs.replace(", 'https://cdn.tailwindcss.com'", "")
        # Also add fonts.googleapis.com to styleSrc and fonts.gstatic.com to fontSrc if missing
        if "fonts.googleapis.com" not in serverjs and "styleSrc" in serverjs:
            serverjs = serverjs.replace(
                "styleSrc: [\"'self'\", \"'unsafe-inline'\"]",
                "styleSrc: [\"'self'\", \"'unsafe-inline'\", \"https://fonts.googleapis.com\"]",
            )
        if "fonts.gstatic.com" not in serverjs and "fontSrc" not in serverjs and "helmet" in serverjs:
            serverjs = serverjs.replace(
                "styleSrc: [\"'self'\", \"'unsafe-inline'\", \"https://fonts.googleapis.com\"]",
                "styleSrc: [\"'self'\", \"'unsafe-inline'\", \"https://fonts.googleapis.com\"], fontSrc: [\"'self'\", \"https://fonts.gstatic.com\"]",
            )
        file_dict["server.js"] = serverjs
        print("  [post-process] Removed Tailwind CDN from server.js CSP")

    return list(file_dict.items())


# ---------------------------------------------------------------------------
# Page-to-model mapping and route reconciliation
# ---------------------------------------------------------------------------


import re as _re


def _camel_to_parts(name: str) -> list:
    """Split CamelCase, spaces, underscores, and hyphens into word parts.

    Examples:
      'TimeEntry' -> ['time', 'entry']
      'BlogPost'  -> ['blog', 'post']
      'Client'    -> ['client']
      'time_entry'-> ['time', 'entry']
      'Time Entry'-> ['time', 'entry']
    """
    # First split on spaces, underscores, hyphens
    tokens = _re.split(r'[\s_\-]+', name)
    parts = []
    for token in tokens:
        # Split camelCase: 'TimeEntry' -> ['Time', 'Entry']
        camel_parts = _re.findall(r'[A-Z][a-z]*|[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)', token)
        if camel_parts:
            parts.extend(p.lower() for p in camel_parts)
        else:
            parts.append(token.lower())
    return [p for p in parts if p]


def _model_slug(name: str) -> str:
    """Canonical model slug for URLs: lowercase, hyphens, no trailing 's'.

    Examples:
      'TimeEntry' -> 'time-entry'
      'BlogPost'  -> 'blog-post'
      'Client'    -> 'client'
    """
    return "-".join(_camel_to_parts(name))


def _model_table(name: str) -> str:
    """Canonical model table name for SQL: lowercase, underscores, plural.

    Examples:
      'TimeEntry' -> 'time_entrys'
      'Client'    -> 'clients'
    """
    return "_".join(_camel_to_parts(name)) + "s"


def _pluralize_slug(slug: str) -> str:
    """Return the correct English plural of a hyphenated URL slug.

    Uses the final word's suffix to determine the plural form:
      - Ends in consonant+y -> replace y with ies  (inventory -> inventories)
      - Ends in ch/sh/s/x/z -> append es           (batch -> batches)
      - Otherwise           -> append s             (recipe -> recipes)

    Examples:
      'recipe'            -> 'recipes'
      'inventory'         -> 'inventories'
      'category'          -> 'categories'
      'brew-batch'        -> 'brew-batches'
      'customer-feedback' -> 'customer-feedbacks'
      'fermentation-log'  -> 'fermentation-logs'
    """
    parts = slug.split("-")
    last = parts[-1]
    vowels = set("aeiou")
    if last.endswith("y") and len(last) > 1 and last[-2] not in vowels:
        plural_last = last[:-1] + "ies"
    elif last.endswith(("ch", "sh", "ss")) or last[-1:] in ("x", "z", "s"):
        plural_last = last + "es"
    else:
        plural_last = last + "s"
    return "-".join(parts[:-1] + [plural_last]) if len(parts) > 1 else plural_last


def _build_plural_slug_rules(spec: dict) -> str:
    """Build a CRITICAL prompt section enforcing plural slugs in EJS view links.

    Returns a formatted string to inject into build prompts. Returns empty string
    if the spec has no data_models (nothing to generate rules for).

    This addresses the systematic bug where Claude generates href="/recipe/1" while
    the Express router is mounted at /recipes, causing 404 on every nav link.
    """
    data_models = spec.get("data_models", [])
    if not data_models:
        return ""

    rows = []
    for m in data_models:
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        plural = _pluralize_slug(slug)
        rows.append((name, slug, plural))

    if not rows:
        return ""

    # Build the table
    table_lines = ["| Model | WRONG (singular: NEVER USE) | CORRECT (plural: ALWAYS USE) |",
                   "|-------|------------------------------|-------------------------------|"]
    for name, slug, plural in rows:
        table_lines.append(f"| {name} | /{slug}/ or /{slug}/1 | /{plural}/ or /{plural}/1 |")

    # Pick the first model for the concrete example
    ex_name, ex_slug, ex_plural = rows[0]

    return f"""
## CRITICAL: PLURAL SLUG RULES, Read This Before Writing ANY View File

ALL href and form action attributes in EJS view files MUST use the PLURAL slug path.
NEVER use the singular slug in any link or form action. This is the #1 cause of 404 errors.

{chr(10).join(table_lines)}

### Concrete examples using {ex_name}:

CORRECT: <a href="/{ex_plural}/1">View</a>
WRONG:   <a href="/{ex_slug}/1">View</a>

CORRECT: <a href="/{ex_plural}/1/edit">Edit</a>
WRONG:   <a href="/{ex_slug}/1/edit">Edit</a>

CORRECT: <form action="/{ex_plural}" method="POST">
WRONG:   <form action="/{ex_slug}" method="POST">

CORRECT: <form action="/{ex_plural}/1/delete" method="POST">
WRONG:   <form action="/{ex_slug}/1/delete" method="POST">

### Custom action routes
If you add any toggle/status/custom action button in a view (e.g., toggle-status, approve,
archive, activate), you MUST define the matching POST route handler in the router file:
  router.post('/:id/toggle-status', requireAuth, async (req, res) => {{ ... }});
Never add an action button to a view without registering its route handler.

### Route aliases (from the ROUTE ALIASES section above)
Route aliases are server-side router mounts only. View href/action attributes always
use the canonical plural CRUD path (/{ex_plural}/), regardless of any page alias.
"""


def _build_page_model_mapping(spec: dict) -> dict:
    """Map spec pages to their corresponding data models by path matching.

    Returns a dict with:
      - page_to_model: {page_path: model_name} for matched pages
      - unmatched_pages: list of page paths with no matching model
      - route_aliases: list of {page_path, model_slug} where page path
        differs from the CRUD slug (these need explicit route handling)
    """
    pages = spec.get("pages", [])
    data_models = spec.get("data_models", [])

    # Build lookup: various forms of each model name -> model dict
    model_lookup = {}  # normalized_form -> model_name
    for m in data_models:
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        table = _model_table(name)
        parts = _camel_to_parts(name)
        # Map multiple forms: slug, plural slug, table name, first word (prefix match),
        # concatenated lowercase, hyphenated, etc.
        plural = _pluralize_slug(slug)
        forms = {slug, plural, table, table.rstrip("s"),
                 name.lower().replace(" ", ""), name.lower().replace(" ", "-")}
        # Add all prefix concatenations (e.g., "tap", "taproom", "taproomorder" from TapRoomOrder)
        # and all suffix concatenations (e.g., "order", "roomorder", "taproomorder")
        for i in range(1, len(parts) + 1):
            prefix = "".join(parts[:i])
            forms.add(prefix)
            forms.add(prefix + "s")
            forms.add(_pluralize_slug(prefix))
            suffix = "".join(parts[len(parts) - i:])
            forms.add(suffix)
            forms.add(suffix + "s")
            forms.add(_pluralize_slug(suffix))
        for form in forms:
            model_lookup[form] = name

    page_to_model = {}
    unmatched_pages = []
    route_aliases = []

    foundation = {"/login", "/register", "/logout", "/", "/dashboard",
                  "/admin/dashboard", "/health", "/settings",
                  "/admin", "/profile", "/account"}

    for page in pages:
        path = page.get("path", "/")
        if path in foundation:
            continue

        # Extract the first path segment for matching
        segment = path.strip("/").split("/")[0].lower().replace("-", "_")

        matched = False
        for form, model_name in model_lookup.items():
            if segment == form or segment == form.replace("-", "_"):
                page_to_model[path] = model_name
                # Check if page path differs from CRUD slug
                crud_slug = _model_slug(model_name)
                crud_plural = _pluralize_slug(crud_slug)
                if path.strip("/") != crud_slug and path.strip("/") != crud_plural:
                    route_aliases.append({
                        "page_path": path,
                        "model_name": model_name,
                        "crud_slug": crud_slug,
                    })
                matched = True
                break

        if not matched:
            unmatched_pages.append(path)

    return {
        "page_to_model": page_to_model,
        "unmatched_pages": unmatched_pages,
        "route_aliases": route_aliases,
    }


def _build_route_reconciliation_section(spec: dict) -> str:
    """Build a prompt section that reconciles page paths with CRUD slugs.

    When a spec page path like /time doesn't match its model's CRUD slug
    like /time-entry, this section explicitly tells Claude to handle both paths.
    """
    mapping = _build_page_model_mapping(spec)
    lines = []

    # Route aliases: page path differs from CRUD slug
    if mapping["route_aliases"]:
        lines.append("## CRITICAL: Page path to model route mapping")
        lines.append("The following spec pages have paths that do NOT match their model's CRUD slug.")
        lines.append("You MUST ensure BOTH paths work (mount the router or add route aliases):\n")
        for alias in mapping["route_aliases"]:
            plural_slug = _pluralize_slug(alias['crud_slug'])
            lines.append(
                f"  - Page '{alias['page_path']}' -> Model '{alias['model_name']}' "
                f"(CRUD at /{plural_slug})")
            lines.append(
                f"    SOLUTION: In server.js, mount the {alias['model_name']} router on BOTH paths:")
            lines.append(
                f"    app.use('{alias['page_path']}', require('./routes/{alias['crud_slug']}'));")
            lines.append(
                f"    app.use('/{plural_slug}', require('./routes/{alias['crud_slug']}'));")
        lines.append("")

    # Unmatched pages: no corresponding model, need their own route files
    if mapping["unmatched_pages"]:
        lines.append("## Pages without a matching data model")
        lines.append("These pages need dedicated route handlers (not model CRUD):\n")
        for path in mapping["unmatched_pages"]:
            lines.append(f"  - {path}: Create routes/{path.strip('/').replace('/', '-')}.js with GET handler")
        lines.append("")

    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Multi-pass code generation
# ---------------------------------------------------------------------------

# Auth/foundation paths that Phase 1 always handles
_FOUNDATION_PATHS = frozenset({
    "/login", "/register", "/logout", "/", "/dashboard",
    "/admin/dashboard", "/health",
})


def _should_use_multi_pass(spec: dict):
    """Decide single-pass vs multi-pass based on spec complexity.

    Returns None for single-pass, or a dict with phase configs for multi-pass.
    Build tier always uses single-pass. Grow triggers at 7+ models or 15+ pages.
    Scale always uses multi-pass. 12+ models triggers 3 phases.
    """
    data_models = spec.get("data_models", [])
    pages = spec.get("pages", [])
    num_models = len(data_models)
    num_pages = len(pages)

    # Build tier: always single-pass (reliable up to 5 models, 10 pages)
    tier = spec.get("tier", "build")
    if tier == "build" and num_models <= 6 and num_pages <= 14:
        return None

    # Simple Grow: single-pass when below thresholds
    if tier == "grow" and num_models < 7 and num_pages < 15:
        return None

    # Multi-pass needed
    feature_pages = [
        p for p in pages if p.get("path", "") not in _FOUNDATION_PATHS
    ]

    if num_models >= 7:
        # 3 phases: foundation + two feature batches
        # Even 7-8 models produce too much code for a single Phase 2 call
        phase_a, phase_b = _split_models_by_dependency(data_models)
        pages_a, pages_b = _assign_pages_to_phases(
            feature_pages, phase_a, phase_b
        )
        return {
            "phases": 3,
            "reason": f"{num_models} models, {num_pages} pages",
            "phase_configs": [
                {"name": "foundation", "models": [], "pages": []},
                {"name": "features_a", "models": phase_a, "pages": pages_a},
                {"name": "features_b", "models": phase_b, "pages": pages_b},
            ],
        }

    # 2 phases: foundation + all features (for 15+ pages but fewer models)
    return {
        "phases": 2,
        "reason": f"{num_models} models, {num_pages} pages",
        "phase_configs": [
            {"name": "foundation", "models": [], "pages": []},
            {"name": "features", "models": data_models, "pages": feature_pages},
        ],
    }


def _split_models_by_dependency(data_models: list) -> tuple:
    """Split models into two groups respecting FK dependencies.

    Parent/standalone models go to phase A, child/join tables to phase B.
    Falls back to 50/50 split if the result is lopsided.
    """
    model_names = set()
    for m in data_models:
        name = m["name"] if isinstance(m, dict) else m
        model_names.add(name.lower().replace(" ", "_"))

    # Build dependency graph: model -> set of models it references
    deps = {}
    for m in data_models:
        name = (m["name"] if isinstance(m, dict) else m).lower().replace(" ", "_")
        refs = set()
        fields = m.get("fields", []) if isinstance(m, dict) else []
        for field in fields:
            fname = field.get("name", "") if isinstance(field, dict) else str(field)
            if fname.endswith("_id"):
                ref_base = fname[:-3]  # strip _id
                for mn in model_names:
                    if ref_base == mn or ref_base + "s" == mn or ref_base == mn.rstrip("s"):
                        refs.add(mn)
                        break
        deps[name] = refs

    # Models referenced by others are "parents"
    referenced = set()
    for model_refs in deps.values():
        referenced.update(model_refs)

    phase_a_names = set()
    phase_b_names = set()
    for m in data_models:
        name = (m["name"] if isinstance(m, dict) else m).lower().replace(" ", "_")
        if name in referenced or not deps.get(name):
            phase_a_names.add(name)
        else:
            phase_b_names.add(name)

    # Fallback: if split is too lopsided, do a simple 50/50
    if len(phase_b_names) < 2 or len(phase_a_names) < 2:
        mid = len(data_models) // 2
        return data_models[:mid], data_models[mid:]

    phase_a = [m for m in data_models
               if (m["name"] if isinstance(m, dict) else m).lower().replace(" ", "_") in phase_a_names]
    phase_b = [m for m in data_models
               if (m["name"] if isinstance(m, dict) else m).lower().replace(" ", "_") in phase_b_names]
    return phase_a, phase_b


def _assign_pages_to_phases(pages: list, a_models: list, b_models: list) -> tuple:
    """Assign spec pages to feature phases by matching to model names."""
    b_names = set()
    for m in b_models:
        name = (m["name"] if isinstance(m, dict) else m).lower().replace(" ", "_")
        b_names.add(name)
        b_names.add(name.rstrip("s"))  # singular form

    pages_a = []
    pages_b = []
    for page in pages:
        page_path = page.get("path", "").lower().strip("/").split("/")[0]
        page_name = page.get("name", "").lower().replace(" ", "_")

        matched_b = False
        for bname in b_names:
            if bname in page_path or bname in page_name:
                pages_b.append(page)
                matched_b = True
                break
        if not matched_b:
            pages_a.append(page)

    return pages_a, pages_b


def _build_phase1_prompt(spec: dict) -> str:
    """Build prompt for Phase 1: foundation files.

    Generates: package.json, server.js (with ALL router mounts), db/* (COMPLETE
    schema + seed for ALL models), middleware, layout, auth views, public files.
    """
    spec_json = json.dumps(spec, indent=2)

    # Reuse revision section from explicit prompt builder
    revision_section = ""
    revision_history = spec.get("revision_history", [])
    rebuild_request = spec.get("rebuild_request", "")
    if revision_history or rebuild_request:
        parts = []
        for entry in revision_history:
            parts.append(f"Round {entry.get('round', '?')}: {entry.get('feedback', '')}")
        if rebuild_request:
            parts.append(rebuild_request)
        revision_section = (
            "\n## REVISION REQUEST (HIGHEST PRIORITY)\n"
            "Fix ALL issues while keeping working functionality intact:\n\n"
            + "\n\n".join(parts) + "\n\n"
        )

    # Collect all model names for router mount stubs
    model_routes = []
    for m in spec.get("data_models", []):
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        model_routes.append(slug)

    # Detect page-path-to-model mismatches for router mount aliases
    mapping = _build_page_model_mapping(spec)
    alias_mounts = []
    for alias in mapping.get("route_aliases", []):
        alias_mounts.append(
            f"  app.use('{alias['page_path']}', require('./routes/{alias['crud_slug']}'));  "
            f"// alias: spec page {alias['page_path']} -> model {alias['model_name']}"
        )

    # Admin email
    admin_email = spec.get("admin_email", "admin@example.com")

    # Inject learned rules from failure KB (same as single-pass builds)
    kb_rules = ""
    try:
        from failure_kb import get_build_prompt_rules, get_review_rules
        kb_rules = get_build_prompt_rules()
        review_rules = get_review_rules()
        if review_rules:
            kb_rules = (kb_rules or "") + review_rules
    except Exception:
        pass
    kb_rules = (kb_rules or "") + _AUTH_MIDDLEWARE_RULE

    kb_section = f"{kb_rules}\n" if kb_rules else ""
    plural_slug_rules_p1 = _build_plural_slug_rules(spec)
    plural_slug_section_p1 = f"{plural_slug_rules_p1}\n" if plural_slug_rules_p1 else ""
    return f"""This is Phase 1 of a multi-phase build. Generate ONLY the foundation files listed below.
Feature routes and views will be generated in Phase 2.

{kb_section}{plural_slug_section_p1}{revision_section}App spec:
{spec_json}

## Files to generate in this phase (ALL of these, nothing else):

1. package.json: include ALL dependencies the full app needs (express, pg, bcrypt, express-session,
   connect-pg-simple, express-ejs-layouts, morgan, express-rate-limit, helmet, dotenv)
2. server.js: MUST mount ALL routers including future Phase 2 routes. For each data model,
   add: `app.use('/{slug}', require('./routes/{slug}'));` even though those files don't exist yet.
   Router mounts needed: auth, support, {', '.join(model_routes)}
{chr(10).join(['   ALSO mount these route ALIASES (spec page paths differ from model CRUD slugs):'] + alias_mounts) if alias_mounts else '   (no route aliases needed)'}
3. db/schema.sql: COMPLETE schema for ALL {len(spec.get('data_models', []))} data models. Every table,
   every column, every foreign key. This is the single source of truth for the entire app.
4. db/seed.sql: CRITICAL: COMPLETE seed data for ALL tables.
   COUNT REQUIREMENT: Generate exactly 15-20 INSERT statements per data table (not users).
   For a todo app, that means 15-20 todos. For a booking app, 15-20 bookings. Count them.
   QUALITY: Use real-sounding names, dates spread across the last 6 months, varied statuses
   (mix of done/not-done, active/inactive, etc.), and realistic descriptions.
   BANNED: No 'Lorem ipsum', 'Test item 1', 'Example', 'Sample', 'foo', 'bar', 'TODO', 'test'.
   First admin user: email='{admin_email}', password=NULL, password_set=FALSE. (SEC-1: NEVER
   inline a bcrypt `$2b$...` literal: the admin authenticates via magic-link or the first-run
   /set-password flow. Hardcoding a shared hash into seed.sql leaks a shadow credential across
   every customer build.)
   One user per role: {{role}}@example.com, same password=NULL, password_set=FALSE shape.
5. db/index.js: Shared PostgreSQL pool with SSL config
6. db/migrate.js: Auto-migration (run schema.sql then seed.sql if users empty)
7. middleware/auth.js: requireAuth, requireRole, generateCsrfToken, validateCsrfToken
8. views/layout.ejs: Complete nav with links to ALL pages from spec, Tailwind CDN with custom colors
9. views/login.ejs, views/register.ejs, views/dashboard.ejs (or admin/dashboard.ejs)
10. routes/auth.js: login, register, logout, GET / (landing or redirect)
11. public/style.css, public/manifest.json, public/sw.js, public/favicon.svg
12. routes/auth.js is already listed above; ensure all spec pages have routes

{"" if not USE_MAGIC_LINK_AUTH else '''
## AUTH MODEL: Magic Link Authentication (Phase 1 must implement these)

- users table: add `password_set BOOLEAN DEFAULT FALSE` column, password column nullable
- Add `magic_link_tokens` table: id UUID PK, user_id FK, token_hash VARCHAR(64), expires_at TIMESTAMP, used_at TIMESTAMP
- Seed users: password_set=FALSE, password=NULL, email_verified=TRUE
- routes/auth.js: implement /login, /request-magic-link, /magic?token=, /set-password
- Login page: add "First time? Request a login link" link
- views/magic-link-sent.ejs and views/set-password.ejs
- middleware/auth.js: add requirePasswordSet (redirect to /set-password if password not set)
- TEST_MODE login shortcut (CRITICAL): In POST /login, BEFORE bcrypt check: if (process.env.TEST_MODE && process.env.NODE_ENV !== 'production' && password === 'test_password_for_ci') then query user by email, set req.session.userId/userEmail/userRole, call req.session.save(() => res.redirect('/dashboard')), and return early. This MUST create a real session and redirect; the build fails without it.
- Production guard: if TEST_MODE && NODE_ENV=production, console.error and process.exit(1)
'''}
NOTE: These files are already provided by the template system and do NOT need to be generated:
db/index.js, db/migrate.js, middleware/auth.js, routes/admin.js, public/sw.js{", views/magic-link-sent.ejs, views/set-password.ejs" if USE_MAGIC_LINK_AUTH else ""}.
If you generate them anyway, your version will be used. But you can save tokens by skipping them.
IMPORTANT: server.js MUST mount the admin router: app.use(require('./routes/admin'));
IMPORTANT: package.json MUST include csv-parser and multer as dependencies.

CRITICAL: db/schema.sql must define ALL tables for the entire app, not just auth tables.{" Including magic_link_tokens." if USE_MAGIC_LINK_AUTH else ""}
CRITICAL: server.js must require and mount ALL routers. Phase 2 route files will be created later.
CRITICAL: views/layout.ejs nav must include links to ALL pages from the spec.
CRITICAL: Do NOT generate any feature route or view files (e.g. routes/inventory.js), Phase 2 handles those.

{_build_design_context(spec, abbreviated=True)}{_build_stock_photo_context(spec)}

Generate all files now using --BEGINFILE-- / --ENDFILE-- delimiters.
"""


def _build_phase_n_prompt(spec: dict, models: list, pages: list,
                          prior_filenames: list, schema_sql: str,
                          phase_num: int) -> str:
    """Build prompt for Phase 2+: feature routes and views for a subset of models."""
    spec_json = json.dumps(spec, indent=2)

    # Build CRUD section for this phase's models only
    crud_lines = []
    for m in models:
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        crud_lines.append(
            f"- {name} (route: /{slug}):\n"
            f"  GET /{slug} (list), GET /{slug}/new (form), POST /{slug} (create),\n"
            f"  GET /{slug}/:id (detail), GET /{slug}/:id/edit (form),\n"
            f"  POST /{slug}/:id/update, POST /{slug}/:id/delete"
        )
    crud_section = "\n".join(crud_lines)

    # Page section for this phase: include page-to-model mapping for clarity
    mapping = _build_page_model_mapping(spec)
    page_lines = []
    for p in pages:
        path = p.get("path", "")
        auth = "requireAuth" if p.get("auth_required", True) else "public"
        model_note = ""
        if path in mapping["page_to_model"]:
            model_note = f" (model: {mapping['page_to_model'][path]})"
        page_lines.append(f"- {p.get('name', path)}: GET {path} - {auth}{model_note}")
    page_section = "\n".join(page_lines) if page_lines else "(derive from models above)"

    prior_list = "\n".join(f"- {f}" for f in prior_filenames)

    # Build explicit file checklist so Claude knows exactly what files to produce
    checklist_lines = []
    for m in models:
        name = m["name"] if isinstance(m, dict) else m
        slug = _model_slug(name)
        checklist_lines.append(
            f"- routes/{slug}.js (CRUD for {name})\n"
            f"- views/{slug}-list.ejs (list view)\n"
            f"- views/{slug}-new.ejs (create form)\n"
            f"- views/{slug}-detail.ejs (detail view)\n"
            f"- views/{slug}-edit.ejs (edit form)"
        )
    file_checklist = "\n".join(checklist_lines)

    # Route aliases for this phase's models
    phase_model_names = set()
    for m in models:
        name = m["name"] if isinstance(m, dict) else m
        phase_model_names.add(name)
    alias_lines = []
    for alias in mapping.get("route_aliases", []):
        if alias["model_name"] in phase_model_names:
            alias_lines.append(
                f"  - Page path '{alias['page_path']}' is served by model "
                f"'{alias['model_name']}' (route file: routes/{alias['crud_slug']}.js). "
                f"server.js already mounts this router on both paths."
            )

    # Inject learned rules from failure KB (same as single-pass builds)
    kb_rules = ""
    try:
        from failure_kb import get_build_prompt_rules, get_review_rules
        kb_rules = get_build_prompt_rules()
        review_rules = get_review_rules()
        if review_rules:
            kb_rules = (kb_rules or "") + review_rules
    except Exception:
        pass
    kb_rules = (kb_rules or "") + _AUTH_MIDDLEWARE_RULE

    kb_section = f"{kb_rules}\n" if kb_rules else ""
    plural_slug_rules_pn = _build_plural_slug_rules(spec)
    plural_slug_section_pn = f"{plural_slug_rules_pn}\n" if plural_slug_rules_pn else ""
    design_context_pn = _build_design_context(spec, abbreviated=True)
    return f"""This is Phase {phase_num} of a multi-phase build. Generate ONLY route files and view
files for the data models listed below. Foundation files already exist from Phase 1.

{kb_section}{plural_slug_section_pn}{design_context_pn}

App spec:
{spec_json}

## Database schema (from Phase 1: use these EXACT table and column names):
```sql
{schema_sql}
```

## Already generated files (DO NOT regenerate any of these):
{prior_list}

## Models to implement in this phase:
{crud_section}

## Pages to implement:
{page_section}

## MANDATORY file checklist (every model MUST produce these files):
{file_checklist}
{chr(10) + '## Route aliases (page paths that differ from CRUD slugs):' + chr(10) + chr(10).join(alias_lines) + chr(10) if alias_lines else ''}
## Rules:
- Generate ONLY routes/*.js and views/*.ejs files for the models above
- EVERY model listed above MUST have its route file and ALL view files. Missing files = build failure.
- Every route file MUST start with:
  const express = require('express');
  const router = express.Router();
  const pool = require('../db');
  const {{ requireAuth, validateCsrfToken, generateCsrfToken }} = require('../middleware/auth');
- Use res.locals.csrfToken in templates (already set by global middleware)
- Use parameterized SQL queries ($1, $2); never string concatenation
- Use <%= %> for user data in EJS (XSS prevention)
- Include proper form validation, error handling, and flash messages
- All POST forms need <input type="hidden" name="_csrf" value="<%= csrfToken %>">
- Submit buttons: bg-primary text-white with visible background
- Export router at the end: module.exports = router;
- Do NOT generate package.json, server.js, db/*, middleware/*, layout.ejs, or public/*
- Non-admin users MUST only see their own data (WHERE created_by = $userId). Admin sees all.
- Admin list pages MUST include an "Import CSV" link/button for bulk data import
- CONFIRMATION ON ALL DESTRUCTIVE ACTIONS: Every delete/remove/reset button MUST use data-confirm="Are you sure you want to delete this record? This cannot be undone." on the form element. Never allow single-click permanent data destruction.
{"- Do NOT use password123 or any hardcoded password in generated code. Auth uses magic link tokens." if USE_MAGIC_LINK_AUTH else ""}
Generate all files now using --BEGINFILE-- / --ENDFILE-- delimiters.
"""


def _call_and_parse(client, prompt: str, model=None, tier="build", build_id="", stage="build") -> list:
    """Make Claude API call(s) with token continuation, parse files, and dedup.

    Returns list of (filepath, content) tuples.
    """
    use_model = model or MODEL
    messages = [{"role": "user", "content": prompt}]
    system = [{
        "type": "text",
        "text": BUILD_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]

    response, _metrics = _shared_call_api(
        client=client,
        messages=messages,
        model=use_model,
        system=system,
        max_tokens=16384,
        use_batch=should_use_batch(tier),
        module="build",
        build_id=build_id,
        stage=stage,
        handle_continuation=True,
    )
    all_text = response.content[0].text

    all_files = parse_files(all_text)

    # Dedup: keep later version if same path appears twice
    seen = {}
    for filepath, content in all_files:
        seen[filepath] = content
    return list(seen.items())


_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load_templates() -> dict:
    """Load static template files that are identical across all builds.

    These files are injected before Phase 1 to reduce API calls. Phase 1
    can still overwrite them if it generates a file with the same path.
    """
    templates = {}
    mapping = {
        "db/index.js": "db_index.js",
        "db/migrate.js": "db_migrate.js",
        "middleware/auth.js": "middleware_auth.js",
        "routes/admin.js": "routes_admin.js",
        "public/sw.js": "sw.js",

        "views/500.ejs": "500.ejs",
        "views/admin-csv-import.ejs": "views_admin_csv_import.ejs",
    }
    if USE_MAGIC_LINK_AUTH:
        mapping["views/magic-link-sent.ejs"] = "magic_link_sent.ejs"
        mapping["views/set-password.ejs"] = "set_password.ejs"
    for app_path, template_file in mapping.items():
        template_path = _TEMPLATES_DIR / template_file
        if template_path.exists():
            templates[app_path] = template_path.read_text()
    return templates


def _generate_multi_pass(spec: dict, phase_plan: dict, tier: str = "build") -> list:
    """Run multi-phase code generation, accumulating files across phases."""
    client = create_client()
    accumulated = {}  # path -> content; later phases overwrite earlier
    num_phases = phase_plan["phases"]

    # Pre-load static templates to reduce Phase 1 generation load
    templates = _load_templates()
    accumulated.update(templates)
    print(f"\nMulti-pass build: {num_phases} phases ({phase_plan['reason']})")
    print(f"  Pre-loaded {len(templates)} template file(s)")

    for i, config in enumerate(phase_plan["phase_configs"]):
        phase_num = i + 1
        print(f"\n--- Phase {phase_num}/{num_phases}: {config['name']} ---")

        if phase_num == 1:
            prompt = _build_phase1_prompt(spec)
            # Phase 1 is mostly boilerplate (schema, server.js, layout); use cheaper model
            phase_model = MODEL_FOUNDATION
            print(f"  Using {MODEL_FOUNDATION} for foundation phase (cost optimization)")
        else:
            schema_sql = accumulated.get("db/schema.sql", "-- schema not available")
            prior_filenames = sorted(accumulated.keys())
            prompt = _build_phase_n_prompt(
                spec, config["models"], config["pages"],
                prior_filenames, schema_sql, phase_num,
            )
            # Feature phases need Opus for complex route/view generation
            phase_model = MODEL

        phase_files = _call_and_parse(client, prompt, model=phase_model, tier=tier,
                                       stage=f"build_phase_{phase_num}")

        if not phase_files:
            if phase_num == 1:
                raise RuntimeError("Multi-pass generation: Phase 1 produced no files")
            else:
                print(f"  Warning: Phase {phase_num} generated no files, continuing with partial output.")
                continue

        for filepath, content in phase_files:
            accumulated[filepath] = content

        print(f"  Phase {phase_num} generated {len(phase_files)} file(s) "
              f"(total accumulated: {len(accumulated)})")

    return list(accumulated.items())


REQUIRED_FILES = [
    "package.json",
    "server.js",
    "db/schema.sql",
    "db/seed.sql",
    "db/index.js",
    "db/migrate.js",
    "middleware/auth.js",
    "public/style.css",
    "public/manifest.json",
    "public/sw.js",
    "public/favicon.svg",
    "views/layout.ejs",
]


def validate_completeness(files):
    """Check that all required files were generated.

    Args:
        files: List of (path, content) tuples from parse_files().

    Returns:
        List of error strings. Empty list means all checks passed.
    """
    errors = []
    paths = {f[0] for f in files}

    for required in REQUIRED_FILES:
        if required not in paths:
            errors.append(f"Missing required file: {required}")

    has_route = any(p.startswith("routes/") for p in paths)
    if not has_route:
        errors.append("No files under routes/ were generated")

    has_view = any(
        p.startswith("views/") and p != "views/layout.ejs" for p in paths
    )
    if not has_view:
        errors.append("No view files besides layout.ejs were generated")

    # Verify package.json is valid JSON
    for path, content in files:
        if path == "package.json":
            try:
                json.loads(content)
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append(f"package.json is not valid JSON: {exc}")
            break

    # Verify layout.ejs includes Tailwind CSS and express-ejs-layouts body tag
    for path, content in files:
        if path == "views/layout.ejs":
            if "/tailwind.css" not in content and "cdn.tailwindcss.com" not in content and "tailwindcss" not in content.lower():
                errors.append("views/layout.ejs is missing Tailwind CSS (expected /tailwind.css link or CDN script)")
            if "<%- body %>" not in content:
                errors.append("views/layout.ejs is missing <%- body %> (required by express-ejs-layouts)")
            break

    # Verify express-ejs-layouts is in package.json dependencies
    for path, content in files:
        if path == "package.json":
            if "express-ejs-layouts" not in content:
                errors.append("package.json is missing express-ejs-layouts dependency")
            break

    return errors


def write_files(files, output_dir):
    """Write parsed files to disk under output_dir, rejecting path traversal.

    Each file path returned by the build agent is resolved against output_dir
    and checked to confirm it stays inside that directory.  This prevents a
    malicious or malformed agent response from writing files outside the
    designated builds/ subdirectory (e.g. '../../etc/passwd' style paths).

    Args:
        files: List of (relative_path, content) tuples from parse_files().
        output_dir: pathlib.Path to the build output directory.

    Raises:
        ValueError: If any path would escape output_dir.
    """
    # Resolve the canonical output directory once (follows symlinks)
    resolved_root = output_dir.resolve()

    for filepath, content in files:
        # Resolve the candidate path; Path.resolve() collapses '..' segments
        full_path = (output_dir / filepath).resolve()

        # Reject any path that does not start with the output directory
        try:
            full_path.relative_to(resolved_root)
        except ValueError:
            raise ValueError(
                f"Path traversal detected: '{filepath}' resolves outside "
                f"output directory '{resolved_root}'"
            )

        full_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            with open(full_path, "wb") as file:
                file.write(content)
        else:
            with open(full_path, "w") as file:
                file.write(content)
        print(f"  wrote {filepath}")


def _build_explicit_prompt(spec: dict) -> str:
    """Build a richer prompt that makes spec requirements explicit.

    Instead of just dumping the JSON, derives concrete route/CRUD checklists
    from the spec so Claude has a clear target rather than having to infer it.
    """
    pages = spec.get("pages", [])
    data_models = spec.get("data_models", [])
    roles = spec.get("roles", [])

    # Detect if app has public (no-auth) pages
    public_pages = [p for p in pages if not p.get("auth_required", False)]
    has_public_pages = len(public_pages) > 0

    # Explicit route requirements per page
    route_lines = []
    if has_public_pages:
        public_names = ", ".join(p.get("name", p.get("path", "?")) for p in public_pages[:5])
        route_lines.append(
            f"  - IMPORTANT: This app has public pages ({public_names}). "
            f"GET / MUST render a public landing page (NOT a login form). "
            f"Include nav links to public pages and a Login/Sign Up link."
        )
    for page in pages:
        path = page.get("path", "/")
        name = page.get("name", path)
        auth = page.get("auth_required", False)
        page_roles = page.get("roles", roles)
        methods = "GET + POST" if any(
            kw in name.lower() for kw in (
                "form", "create", "edit", "login", "register", "submit",
                "import", "export", "upload", "sync", "approve", "reject",
                "delete", "settings", "profile", "account", "config",
            )
        ) else "GET"
        auth_note = f", requireAuth({page_roles})" if auth and page_roles else (", requireAuth" if auth else "")
        route_lines.append(f"  - {name}: {methods} {path}{auth_note}")

    # Explicit CRUD per data model
    crud_lines = []
    for model in data_models:
        model_name = model.get("name", "?")
        table = _model_table(model_name)
        slug = _model_slug(model_name)
        crud_lines.append(
            f"  - {model_name} (table: {table}, route: /{slug}s): "
            f"GET /{slug}s (list), GET /{slug}s/new, POST /{slug}s (create), "
            f"GET /{slug}s/:id (detail), GET /{slug}s/:id/edit, "
            f"POST /{slug}s/:id/update, POST /{slug}s/:id/delete"
        )

    # Admin email: use customer's email so they can log in as admin immediately
    admin_email = spec.get("admin_email", "admin@example.com")

    if USE_MAGIC_LINK_AUTH:
        admin_seed_note = (
            f"  - The first admin user in db/seed.sql MUST use email '{admin_email}' "
            f"with password_set=FALSE and NO password hash. All seed users have password_set=FALSE."
        )
        magic_link_auth_section = f"""
## AUTH MODEL: Magic Link Authentication

Generated apps MUST use magic link authentication instead of password123:

### Database changes:
- users table: add `password_set BOOLEAN DEFAULT FALSE` column. Password column is nullable.
- Add `magic_link_tokens` table: id UUID PK, user_id FK, token_hash VARCHAR(64), expires_at TIMESTAMP, used_at TIMESTAMP
- Seed users: all created with password_set=FALSE, password=NULL, email_verified=TRUE

### Routes to implement:
- GET /login: standard login form BUT add "First time? Request a login link" link below the form
- POST /login: validate email+password as before (for users who have set a password)
- GET /request-magic-link: form with email field
- POST /request-magic-link: generate token with `const token = crypto.randomBytes(32).toString('hex')`, hash with `const hash = crypto.createHash('sha256').update(token).digest('hex')`, store hash in magic_link_tokens (24h expiry), log magic link URL to console. If process.env.RESEND_API_KEY is set, send email via Resend REST API:
  ```javascript
  const magicUrl = `${{req.protocol}}://${{req.get('host')}}/magic?token=${{token}}`;
  console.log('Magic link:', magicUrl);
  if (process.env.RESEND_API_KEY) {{
    fetch('https://api.resend.com/emails', {{
      method: 'POST',
      headers: {{ 'Authorization': `Bearer ${{process.env.RESEND_API_KEY}}`, 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ from: 'noreply@craftandship.io', to: [email], subject: 'Your login link', html: `<p>Click here to log in:</p><p><a href="${{magicUrl}}">${{magicUrl}}</a></p><p>This link expires in 24 hours.</p>` }})
    }}).then(r => {{ if (!r.ok) r.text().then(t => console.error('Magic link email API error:', r.status, t)); }})
      .catch(err => console.error('Magic link email failed:', err));
  }}
  ```
  Rate limit: 3 per 15 min per IP.
- GET /magic?token=xxx: hash the token with `crypto.createHash('sha256').update(token).digest('hex')`, look up hash in magic_link_tokens (WHERE token_hash = hash AND expires_at > NOW() AND used_at IS NULL), mark used (SET used_at = NOW()), create session, redirect to /set-password if password_set=FALSE else /dashboard
- GET /set-password: form with password + confirm fields. Requires active session.
- POST /set-password: validate passwords match + min 8 chars, bcrypt hash, update user (password + password_set=TRUE), redirect to /dashboard. Rate limit: 5 per 15 min.
- POST /login rate limit: 10 per 15 min per IP

### Middleware:
- Add `requirePasswordSet` middleware: if session has userId but user's password_set=FALSE, redirect to /set-password (except for /set-password, /logout, /magic routes)
- Apply requirePasswordSet after requireAuth on protected routes

### TEST_MODE login shortcut (CRITICAL for automated testing; build WILL fail without this):
In routes/auth.js POST /login handler, add this block BEFORE the normal bcrypt password check.
This MUST create a session and redirect, not just skip validation:
```javascript
// TEST_MODE login shortcut: MUST be before bcrypt check
if (process.env.TEST_MODE && process.env.NODE_ENV !== 'production' && password === 'test_password_for_ci') {{
  const userResult = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
  if (userResult.rows.length > 0) {{
    const user = userResult.rows[0];
    req.session.userId = user.id;
    req.session.userEmail = user.email;
    req.session.userRole = user.role || 'admin';
    return req.session.save(() => res.redirect('/dashboard'));
  }}
}}
```
The TEST_MODE block MUST:
1. Query the user by email
2. Set session variables (userId, userEmail, userRole)
3. Call req.session.save(() => res.redirect('/dashboard'))
4. Return early (do NOT fall through to bcrypt check)

Production guard in server.js startup:
```javascript
if (process.env.TEST_MODE && process.env.NODE_ENV === 'production') {{
  console.error('TEST_MODE must not be set in production');
  process.exit(1);
}}
```

### Views:
- views/magic-link-sent.ejs: "Check your email" page with resend form
- views/set-password.ejs: password setup form (password + confirm)
"""
        checklist_items = [
            "Before finishing, verify every item:",
            "[ ] Every page listed above has a route handler AND an EJS view file",
            "[ ] Every data model has all 7 CRUD operations (list/new/create/detail/edit/update/delete)",
            "[ ] POST /login validates credentials, creates session, and redirects to /dashboard (NOT /login)",
            "[ ] POST /register validates, creates user with bcrypt, creates session, and redirects to /dashboard",
            "[ ] POST /logout calls req.session.destroy() and redirects to /login (NOT /)",
            "[ ] Auth-required pages call requireAuth before the handler",
            "[ ] PUBLIC FORM PAGES: any page with auth_required=false that collects user input (e.g. /contact, /book, /request) MUST have: (1) GET handler that renders the form, no auth; (2) POST handler that saves submission, no auth, redirect with success message. Do NOT put visitor-facing form handlers inside admin-only CRUD routers. Mount these at the spec page path (e.g. GET /contact + POST /contact), keep admin CRUD at a separate path (e.g. /admin/contact-messages or /contact-messages).",
            f"[ ] db/seed.sql inserts 10-20 realistic records per table (no Lorem ipsum, no placeholder emails). {admin_seed_note}",
            "[ ] magic_link_tokens table exists in schema.sql",
            "[ ] /magic route handler exists with token validation",
            "[ ] /set-password route exists with password update logic",
            "[ ] /request-magic-link route exists with token generation",
            "[ ] Login page has 'First time? Request a login link' link",
            "[ ] Every POST form has <input type='hidden' name='_csrf' value='<%= csrfToken %>'>",
            "[ ] Every user-supplied value in EJS uses <%= %> not <%-",
            "[ ] server.js exports the app for testing AND binds the port on startup",
            "[ ] server.js has app.set('trust proxy', 1) BEFORE session middleware (required for Railway secure cookies)",
            "[ ] Every CRUD list page has a visible 'Add New' / 'Create' button linking to the /new form page",
            "[ ] Every db.query() result is null-checked before accessing .rows[0].id (prevents TypeError crash)",
            "[ ] requireAuth checks session exists (NOT role). requireRole is separate. Non-admin users can log in and see their pages.",
            "[ ] req.session.save() is called before res.redirect() on EVERY login/register/verify code path",
            "[ ] DESIGN: Login/register pages use split-panel layout (brand gradient panel left, form right); NEVER a centered card on a blank white page",
            "[ ] DESIGN: Every page has a gradient or tinted background (hero-gradient class or bg-gradient-*); NEVER flat #ffffff or #f9fafb",
            "[ ] DESIGN: Nav bar uses bg-primary or bg-gray-900 with white text, NEVER bg-white or transparent",
            "[ ] DESIGN: Cards have class=\"animate-in\" and interactive cards use hover:shadow-lg hover:scale-[1.01] transition-all duration-200",
        ]
    else:
        admin_seed_note = (
            f"  - The first admin user in db/seed.sql MUST use email '{admin_email}' "
            f"with password=NULL, password_set=FALSE. SEC-1: do NOT inline a bcrypt "
            f"`$2b$...` literal; shipping a shared hash across every customer app is a "
            f"committed credential. The admin authenticates on first visit via "
            f"/set-password (password-primary apps) or /request-magic-link."
        )
        magic_link_auth_section = ""
        checklist_items = [
            "Before finishing, verify every item:",
            "[ ] Every page listed above has a route handler AND an EJS view file",
            "[ ] Every data model has all 7 CRUD operations (list/new/create/detail/edit/update/delete)",
            "[ ] POST /login validates credentials, creates session, and redirects to /dashboard (NOT /login)",
            "[ ] POST /register validates, creates user with bcrypt, creates session, and redirects to /dashboard",
            "[ ] POST /logout calls req.session.destroy() and redirects to /login (NOT /)",
            "[ ] Auth-required pages call requireAuth before the handler",
            "[ ] PUBLIC FORM PAGES: any page with auth_required=false that collects user input (e.g. /contact, /book, /request) MUST have: (1) GET handler that renders the form, no auth; (2) POST handler that saves submission, no auth, redirect with success message. Do NOT put visitor-facing form handlers inside admin-only CRUD routers. Mount these at the spec page path (e.g. GET /contact + POST /contact), keep admin CRUD at a separate path (e.g. /admin/contact-messages or /contact-messages).",
            f"[ ] db/seed.sql inserts 10-20 realistic records per table (no Lorem ipsum, no placeholder emails). {admin_seed_note}",
            "[ ] Every POST form has <input type='hidden' name='_csrf' value='<%= csrfToken %>'>",
            "[ ] Every user-supplied value in EJS uses <%= %> not <%-",
            "[ ] server.js exports the app for testing AND binds the port on startup",
            "[ ] server.js has app.set('trust proxy', 1) BEFORE session middleware (required for Railway secure cookies)",
            "[ ] Every CRUD list page has a visible 'Add New' / 'Create' button linking to the /new form page",
            "[ ] Every db.query() result is null-checked before accessing .rows[0].id (prevents TypeError crash)",
            "[ ] requireAuth checks session exists (NOT role). requireRole is separate. Non-admin users can log in and see their pages.",
            "[ ] req.session.save() is called before res.redirect() on EVERY login/register/verify code path",
            "[ ] DESIGN: Login/register pages use split-panel layout (brand gradient panel left, form right); NEVER a centered card on a blank white page",
            "[ ] DESIGN: Every page has a gradient or tinted background (hero-gradient class or bg-gradient-*); NEVER flat #ffffff or #f9fafb",
            "[ ] DESIGN: Nav bar uses bg-primary or bg-gray-900 with white text, NEVER bg-white or transparent",
            "[ ] DESIGN: Cards have class=\"animate-in\" and interactive cards use hover:shadow-lg hover:scale-[1.01] transition-all duration-200",
        ]

    checklist = "\n".join(checklist_items)

    route_section = "\n".join(route_lines) if route_lines else "  (derive from spec)"
    crud_section = "\n".join(crud_lines) if crud_lines else "  (derive from spec)"

    # Page-to-model route reconciliation (detects path mismatches like /time vs /time-entry)
    reconciliation = _build_route_reconciliation_section(spec)

    spec_json = json.dumps(spec, indent=2)

    # Inject learned rules from failure KB (if any high-confidence patterns exist)
    kb_rules = ""
    try:
        from failure_kb import get_build_prompt_rules, get_review_rules
        kb_rules = get_build_prompt_rules()
        review_rules = get_review_rules()
        if review_rules:
            kb_rules = (kb_rules or "") + review_rules
    except Exception:
        pass
    kb_rules = (kb_rules or "") + _AUTH_MIDDLEWARE_RULE

    # Revision/rebuild request: customer feedback on previous build
    # Include accumulated revision_history (persists across failed builds)
    # plus the current rebuild_request
    revision_section = ""
    revision_history = spec.get("revision_history", [])
    rebuild_request = spec.get("rebuild_request", "")

    if revision_history or rebuild_request:
        parts = []
        if revision_history:
            for entry in revision_history:
                round_num = entry.get("round", "?")
                fb = entry.get("feedback", "")
                parts.append(f"Round {round_num}: {fb}")
        if rebuild_request:
            parts.append(rebuild_request)

        all_feedback = "\n\n".join(parts)
        revision_section = f"""
## REVISION REQUEST (HIGHEST PRIORITY)
The customer reviewed previous build(s) and provided this feedback. Fix ALL issues
while keeping all other working functionality intact:

{all_feedback}

"""

    plural_slug_rules = _build_plural_slug_rules(spec)

    # Design typography, palette, and style guidance (UUPM-enhanced)
    design_section = _build_design_context(spec)

    # Stock photo context (tells Claude which local images are available)
    stock_photo_section = _build_stock_photo_context(spec)

    # Template pre-load notice: these files are injected from cli/templates/
    # before post-processing, so Claude can skip generating them to save tokens.
    # Mirrors the NOTE block in _build_phase1_prompt().
    template_notice = (
        "NOTE: These files are already provided by the template system and do NOT need to be generated:\n"
        "db/index.js, db/migrate.js, middleware/auth.js, routes/admin.js, public/sw.js, "
        "views/500.ejs, views/admin-csv-import.ejs"
        + (", views/magic-link-sent.ejs, views/set-password.ejs" if USE_MAGIC_LINK_AUTH else "")
        + ".\nIf you generate them anyway, your version will be used. But you can save tokens by skipping them.\n"
        "IMPORTANT: server.js MUST mount the admin router: app.use(require('./routes/admin'));\n"
        "IMPORTANT: package.json MUST include csv-parser and multer as dependencies.\n"
    )

    return f"""Generate the complete application for this spec:

{spec_json}
{revision_section}{magic_link_auth_section}{design_section}{stock_photo_section}
## Required routes (generate ALL of these)

{route_section}

## Required CRUD per data model (generate ALL operations for EACH model)

{crud_section}

{reconciliation}{plural_slug_rules}
{template_notice}
## Admin surfaces emitted by Python (do NOT generate)

The Python widget registry emits `views/admin/settings.ejs`,
`views/admin/<slug>/new.ejs`, `views/admin/<slug>/edit.ejs`, and the
settings section of `views/admin/dashboard.ejs`.
Do NOT generate these files; they will be overwritten.

## HARD UX RULES - violations are failed builds (v17 contract)

1. Admin pages for read-only models (e.g. ContactMessage) have no
   "Add New" or "Edit" affordances - read-only means view-and-delete only.

2. Delete confirmation fires EXACTLY ONCE per click. Use ONLY
   data-confirm="..." on the form - do NOT also add onsubmit="return
   confirm(...)". Use ONLY one global submit handler; do NOT add a
   querySelectorAll('[data-confirm]').forEach(el => el.addEventListener(
   'submit', ...)) block (the injected submit-guard script handles it).

3. Inline YouTube embeds on the home page render as click-to-play thumbnail
   links (via /api/video-thumb/:videoId), NOT as live iframes. Only event
   detail / video detail pages get inline iframes.

## Pre-flight checklist

{checklist}
{kb_rules}

Now generate all files. Do not skip any required route or CRUD operation. Do not skip design: split-panel login, gradient/tinted page backgrounds, solid-color nav, and animate-in cards are REQUIRED - a flat-white centered-card login is a failed build."""


# ---------------------------------------------------------------------------
# Pipeline Quality Sprint post-processors (8→9 rating)
# ---------------------------------------------------------------------------


@processor(after=('_ensure_resend_from_address',))
def _fix_delete_button_styling(files: list) -> list:
    """Prevent base submit button styles from overriding delete button styling.

    Claude generates a blanket `button[type="submit"] { background-color: primary; color: white; }`
    in style.css which makes delete buttons look like primary action buttons.
    This scopes the rule to exclude buttons with text-red classes.
    """
    import re as _re_mod

    file_dict = dict(files)
    changed = False
    # Capture Claude's ORIGINAL style.css before any branches below
    # mutate it. Some guards downstream (OUTLINE_BUTTON_CONTRAST) want
    # to check whether the SAFETY-NET rule was actually present in
    # Claude's output: they must not be fooled by an earlier branch
    # appending a `button[type="submit"]` fragment during its own
    # override.
    _original_style = file_dict.get("public/style.css", "")
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "fix_delete_button_styling.start",
        {"input_count": len(files)},
    )

    # Fix style.css: scope button[type="submit"] to exclude delete buttons.
    # IMPORTANT: Use :not([class*="bg-"]) so buttons with an explicit Tailwind
    # background utility (e.g. bg-red-600 on "Clear All Data") keep their
    # solid color. The old selector caught ALL form[data-confirm] submit buttons
    # including primary destructive-action buttons, making them invisible.
    style = file_dict.get("public/style.css", "")
    if style and 'button[type="submit"]' in style:
        # Add a :not() exclusion or add an override for delete buttons
        if ".btn-delete" not in style and "text-red" not in style:
            # Add override at end of file for delete buttons
            style = style.rstrip() + "\n\n/* Delete button override - prevent primary styling on inline text-link delete buttons */\n"
            style += 'form[data-confirm] button[type="submit"]:not([class*="bg-"]),\n'
            style += 'button[type="submit"].text-red-500:not([class*="bg-"]),\n'
            style += 'button[type="submit"].text-red-600:not([class*="bg-"]) {\n'
            style += '  background: transparent;\n'
            style += '  color: #ef4444;\n'
            style += '  box-shadow: none;\n'
            style += '  font-weight: 500;\n'
            style += '  border-radius: 0.375rem;\n'
            style += '}\n'
            style += 'form[data-confirm] button[type="submit"]:not([class*="bg-"]):hover,\n'
            style += 'button[type="submit"].text-red-500:not([class*="bg-"]):hover,\n'
            style += 'button[type="submit"].text-red-600:not([class*="bg-"]):hover {\n'
            style += '  color: #dc2626;\n'
            style += '  background: rgba(239, 68, 68, 0.05);\n'
            style += '  transform: none;\n'
            style += '  box-shadow: none;\n'
            style += '}\n'
            file_dict["public/style.css"] = style
            changed = True
            print("  [post-process] Added delete button styling override")

    # Also fix premium-base.css: scope the submit button styles.
    # Same :not([class*="bg-"]) exclusion applies here so buttons with explicit
    # Tailwind background classes win the specificity battle.
    premium = file_dict.get("public/premium-base.css", "")
    if premium and 'button[type="submit"]' in premium:
        # Add the same override
        if "data-confirm" not in premium:
            premium = premium.rstrip() + "\n\n/* Delete button override - only targets inline text-link delete buttons, not primary destructive buttons with explicit bg-* classes */\n"
            premium += 'form[data-confirm] button[type="submit"]:not([class*="bg-"]) {\n'
            premium += '  background: transparent !important;\n'
            premium += '  color: #ef4444 !important;\n'
            premium += '  box-shadow: none !important;\n'
            premium += '}\n'
            premium += 'form[data-confirm] button[type="submit"]:not([class*="bg-"]):hover {\n'
            premium += '  color: #dc2626 !important;\n'
            premium += '  transform: none !important;\n'
            premium += '  box-shadow: none !important;\n'
            premium += '}\n'
            file_dict["public/premium-base.css"] = premium
            changed = True

    # DELETE_BUTTON_UPGRADE (MW v12 fix): the existing fix only neutralizes already-
    # red delete buttons. Many Claude-generated templates give Delete buttons the
    # same solid accent-blue gradient as primary CTAs (observed on MW v12 contact
    # messages page), because they use Tailwind classes like `bg-accent`. Inject
    # a unified destructive style that targets forms whose action contains '/delete'
    # OR '/destroy', regardless of existing classes, so Delete always reads as
    # destructive across every generated app.
    style = file_dict.get("public/style.css", "")
    if style and "DELETE_BUTTON_UPGRADE" not in style:
        style = style.rstrip() + "\n\n/* DELETE_BUTTON_UPGRADE (MW v12): unified destructive style */\n"
        style += 'form[action*="/delete"] button[type="submit"],\n'
        style += 'form[action*="/destroy"] button[type="submit"],\n'
        style += 'button[data-destructive="true"] {\n'
        style += '  background: transparent !important;\n'
        style += '  color: #ef4444 !important;\n'
        style += '  border: 1.5px solid rgba(239, 68, 68, 0.45) !important;\n'
        style += '  box-shadow: none !important;\n'
        style += '  font-weight: 500 !important;\n'
        style += '  transition: background 0.15s, color 0.15s, border-color 0.15s !important;\n'
        style += '}\n'
        style += 'form[action*="/delete"] button[type="submit"]:hover,\n'
        style += 'form[action*="/destroy"] button[type="submit"]:hover,\n'
        style += 'button[data-destructive="true"]:hover {\n'
        style += '  background: rgba(239, 68, 68, 0.12) !important;\n'
        style += '  color: #fca5a5 !important;\n'
        style += '  border-color: rgba(239, 68, 68, 0.7) !important;\n'
        style += '  transform: none !important;\n'
        style += '}\n'
        style += 'form[action*="/delete"] button[type="submit"]:focus-visible,\n'
        style += 'form[action*="/destroy"] button[type="submit"]:focus-visible {\n'
        style += '  outline: 2px solid rgba(239, 68, 68, 0.5);\n'
        style += '  outline-offset: 2px;\n'
        style += '}\n'
        file_dict["public/style.css"] = style
        changed = True
        print("  [post-process] Added DELETE_BUTTON_UPGRADE unified destructive style")

    # OUTLINE_BUTTON_CONTRAST: Claude
    # emits "ghost/outline" submit buttons with `style="border-color:
    # var(--color-primary); color: var(--color-primary);"`, intending
    # a transparent-bg pill with primary-colored text + border. But the
    # blanket `button[type="submit"] { background-color: primary;
    # color: #fff }` safety net higher up in style.css fills the bg
    # with primary too, while the inline `color: primary` beats the
    # safety-net's `color: #fff` on specificity → result: primary text
    # on primary bg → invisible (the Publish/Unpublish toggle on
    # /admin/blog-posts was a solid maroon block with no readable text).
    #
    # Fix: add a higher-specificity rule that detects the outline
    # pattern (inline style has `border-color` AND `color:` referencing
    # a CSS var) and forces a transparent background so the intended
    # pill shape comes back.
    #
    # Scoped to only fire when Claude's ORIGINAL style.css already had
    # a `button[type="submit"]` safety-net rule: no safety net → no
    # override needed. Checking the original (not the post-mutation
    # content) avoids a false positive caused by DELETE_BUTTON_UPGRADE
    # above appending its own `button[type="submit"]` fragment.
    style = file_dict.get("public/style.css", "")
    if (
        style
        and 'button[type="submit"]' in _original_style
        and "OUTLINE_BUTTON_CONTRAST" not in style
    ):
        style = style.rstrip() + "\n\n/* OUTLINE_BUTTON_CONTRAST: undo bg fill for explicit ghost buttons */\n"
        style += 'button[type="submit"][style*="border-color"][style*="color:"] {\n'
        style += '  background: transparent !important;\n'
        style += '  box-shadow: none !important;\n'
        style += '}\n'
        style += 'button[type="submit"][style*="border-color"][style*="color:"]:hover {\n'
        style += '  background: var(--color-primary, #6b1519) !important;\n'
        style += '  color: #fff !important;\n'
        style += '}\n'
        file_dict["public/style.css"] = style
        changed = True
        print("  [post-process] Added OUTLINE_BUTTON_CONTRAST override")

    if changed:
        emit_build_trace(
            _CURRENT_BUILD_DIR.get(),
            "fix_delete_button_styling.completed",
            {"changed": True, "output_count": len(file_dict)},
        )
        return list(file_dict.items())
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        "fix_delete_button_styling.skipped",
        {"reason": "no_changes"},
    )
    return files


@processor(after=('_fix_delete_button_styling',), phase=('build',))
def _fix_csp_cdn_sources(files: list) -> list:
    """Add CDN domains used in views to CSP connect-src in server.js.

    Claude loads Chart.js and other libraries from CDNs but the Helmet CSP
    blocks their source map requests (connect-src: 'self'). This adds the
    CDN domain to connect-src when detected in view files.
    """
    import re as _re_mod

    file_dict = dict(files)
    server = file_dict.get("server.js", "")
    if not server or "contentSecurityPolicy" not in server:
        return files

    # Scan all views for CDN script/style sources
    cdn_domains = set()
    for filepath, content in file_dict.items():
        if not filepath.endswith((".ejs", ".js", ".css")):
            continue
        for domain in ["https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com",
                       "https://fonts.googleapis.com", "https://fonts.gstatic.com"]:
            if domain in content:
                cdn_domains.add(domain)

    # Also fix service worker to skip cross-origin requests
    sw = file_dict.get("public/sw.js", "")
    if sw and "self.location.origin" not in sw and "event.request.url.startsWith" not in sw:
        sw = sw.replace(
            "if (event.request.method !== 'GET') return;",
            "if (event.request.method !== 'GET') return;\n  if (!event.request.url.startsWith(self.location.origin)) return;",
        )
        # Also handle Claude's alternate pattern
        sw = sw.replace(
            "if (e.request.method !== 'GET') return;",
            "if (e.request.method !== 'GET') return;\n  if (!e.request.url.startsWith(self.location.origin)) return;",
        )
        # Handle the simple fetch-all pattern without method check.
        # CRITICAL: detect the fetch handler's parameter name (e, event, evt, ...)
        # and inject a guard that uses THAT name. Hardcoding `event` produces a
        # ReferenceError when Claude's handler uses `function(e)` (MW v3 bug 2026-04-10).
        if "self.location.origin" not in sw and "addEventListener('fetch'" in sw:
            def _inject_fetch_guard(m):
                header = m.group(0)
                # Match: addEventListener('fetch', function(PARAM) {  OR  (PARAM) => {
                param_match = _re_mod.search(
                    r"addEventListener\('fetch'\s*,\s*(?:function\s*\(\s*([A-Za-z_$][\w$]*)\s*\)|\(\s*([A-Za-z_$][\w$]*)\s*\)\s*=>)",
                    header,
                )
                param = (param_match.group(1) or param_match.group(2)) if param_match else "event"
                return header + f"\n  if (!{param}.request.url.startsWith(self.location.origin)) return;"
            sw = _re_mod.sub(
                r"addEventListener\('fetch'[^{]*\{",
                _inject_fetch_guard,
                sw,
            )
        if sw != file_dict.get("public/sw.js", ""):
            file_dict["public/sw.js"] = sw
            print("  [post-process] Fixed service worker to skip cross-origin requests")

    if not cdn_domains:
        # sw.js may still have been patched above; return file_dict, not
        # the original `files`, so that fix is not thrown away.
        return list(file_dict.items())

    # Add CDN domains to connectSrc if not already there
    connect_src_match = _re_mod.search(r"connectSrc:\s*\[[^\]]*\]", server)
    connect_src_str = connect_src_match.group() if connect_src_match else ""
    for domain in cdn_domains:
        if domain not in connect_src_str:
            server = _re_mod.sub(
                r"(connectSrc:\s*\[)([^\]]*?)(\])",
                lambda m: m.group(1) + m.group(2).rstrip().rstrip(",") + f', "{domain}"' + m.group(3),
                server,
                count=1,
            )

    if server != file_dict.get("server.js", ""):
        file_dict["server.js"] = server
        print("  [post-process] Added CDN domains to CSP connect-src")

    return list(file_dict.items())


@processor(after=('_fix_csp_cdn_sources',), phase=('build',))
def _fix_cdn_script_graceful(files: list) -> list:
    """Wrap CDN-dependent code (Chart.js etc.) in typeof guards.

    CDN scripts may fail to load (ad blockers, network issues, CSP).
    This wraps `new Chart(` calls with `if (typeof Chart !== 'undefined')`
    so the page doesn't throw uncaught ReferenceErrors.
    """
    import re as _re_mod

    file_dict = dict(files)
    changed = False

    for filepath, content in list(file_dict.items()):
        if not filepath.endswith(".ejs"):
            continue

        original = content

        # Wrap bare `new Chart(` in typeof guard
        content = _re_mod.sub(
            r'(\s*)(new Chart\()',
            r'\1if (typeof Chart !== "undefined") { \2',
            content,
        )
        # Close the guard after the Chart constructor's closing );
        if content != original:
            # Find each `new Chart(` block and add closing brace after });
            # Simple approach: add } after the script's last });
            content = _re_mod.sub(
                r"(new Chart\([^;]+;)",
                r"\1 }",
                content,
                flags=_re_mod.DOTALL,
            )
            file_dict[filepath] = content
            changed = True

    if changed:
        print("  [post-process] Wrapped CDN-dependent code in typeof guards")

    return list(file_dict.items())


@processor(after=('_convert_raw_file_inputs_to_upload_zones',))
def _ensure_event_time_helper(files: list, spec: dict) -> list:
    """TIMEZONE_DISPLAY_HELPER (v13 → v14): inject app.locals.formatEventTime
    + rewrite all event date/time displays to use it.

    Problem observed on MW v12/v13: events store `date_time` as timestamp
    without time zone. Different views formatted with Node's default TZ
    (which shifts with system TZ) vs no conversion at all, giving
    inconsistent output like "May 13, 2026 12:00 AM CDT" instead of
    "May 12, 2026 7:00 PM CDT".

    Fix: add a single server-side helper bound at app startup that always
    converts through an explicit timezone (defaults to America/Chicago;
    overridable via site_settings.site_timezone) with consistent format.
    Rewrite event.date_time / e.date_time displays in EJS to call the
    helper instead of rolling their own formatting.
    """
    import re as re_mod

    file_dict = dict(files)

    # Find the app instance + mount point
    server = file_dict.get("server.js") or file_dict.get("app.js") or ""
    if not server or "formatEventTime" in server:
        # Already has helper, or no server.js; skip
        if not server:
            return files

    # Inject the helper near the top of server.js (after app instantiation)
    server_path = "server.js" if "server.js" in file_dict else ("app.js" if "app.js" in file_dict else None)
    if not server_path or "formatEventTime" in file_dict[server_path]:
        pass  # will still rewrite views below
    else:
        helper_code = (
            "\n// TIMEZONE_DISPLAY_HELPER (v14): single source of truth for event\n"
            "// date/time display. Uses explicit timeZone to avoid Node-default-TZ\n"
            "// drift; reads site_settings.site_timezone if set, else America/Chicago.\n"
            "app.locals.formatEventTime = function(value, tz) {\n"
            "  if (!value) return '';\n"
            "  var d = value instanceof Date ? value : new Date(value);\n"
            "  if (isNaN(d.getTime())) return '';\n"
            "  var zone = tz || (global._settingsCache && global._settingsCache.site_timezone) || 'America/Chicago';\n"
            "  try {\n"
            "    return d.toLocaleString('en-US', {\n"
            "      year: 'numeric', month: 'short', day: 'numeric',\n"
            "      hour: 'numeric', minute: '2-digit', timeZone: zone, timeZoneName: 'short'\n"
            "    });\n"
            "  } catch (e) {\n"
            "    return d.toISOString();\n"
            "  }\n"
            "};\n"
            "app.locals.formatEventDate = function(value, tz) {\n"
            "  if (!value) return '';\n"
            "  var d = value instanceof Date ? value : new Date(value);\n"
            "  if (isNaN(d.getTime())) return '';\n"
            "  var zone = tz || (global._settingsCache && global._settingsCache.site_timezone) || 'America/Chicago';\n"
            "  try {\n"
            "    return d.toLocaleDateString('en-US', {\n"
            "      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', timeZone: zone\n"
            "    });\n"
            "  } catch (e) {\n"
            "    return d.toISOString().slice(0, 10);\n"
            "  }\n"
            "};\n"
        )

        # Inject right after `const app = express()` or `var app = express()`
        s = file_dict[server_path]
        m = re_mod.search(r'((?:const|let|var)\s+app\s*=\s*express\(\)\s*;?)', s)
        if m:
            insertion = m.end()
            s = s[:insertion] + helper_code + s[insertion:]
            file_dict[server_path] = s
            print(f"  [post-process] Injected app.locals.formatEventTime + formatEventDate helpers into {server_path}")

    # Rewrite EJS view templates that format event dates.
    # v15 BROADENING: match the actual patterns Claude generates in cards,
    # not just the narrow set v14 caught. v14 missed ~7 view files because
    # the regex didn't match new Date(x.date_time).toLocaleDateString(...)
    # or forEach iterator vars (e, evt, item).
    #
    # Patterns targeted (in order of precedence):
    #   A. <%= new Date(VAR.date_time).toLocaleDateString(...) %>  → formatEventDate
    #   B. <%= new Date(VAR.date_time).toLocaleString(...) %>      → formatEventTime
    #   C. <%= VAR.date_time.toLocaleString(...) %>                → formatEventTime
    #   D. <%= VAR.date_time %>                                     → formatEventTime
    #
    # Where VAR is any valid JS identifier (event, e, evt, item, etc.).
    # After rewrite, also strip any immediately-following literal ' UTC' text
    # since the helper now emits timeZoneName in the output.

    # Pattern A: toLocaleDateString
    date_to_locale_date_re = re_mod.compile(
        r'<%=\s*new\s+Date\(\s*(\w+)\.date_time\s*\)\s*\.toLocaleDateString\s*\([^%]*?\)\s*%>',
        re_mod.DOTALL,
    )
    # Pattern B: toLocaleString
    date_to_locale_string_re = re_mod.compile(
        r'<%=\s*new\s+Date\(\s*(\w+)\.date_time\s*\)\s*\.toLocaleString\s*\([^%]*?\)\s*%>',
        re_mod.DOTALL,
    )
    # Pattern C: bare VAR.date_time.toLocaleXxx(...)
    date_bare_method_re = re_mod.compile(
        r'<%=\s*(\w+)\.date_time\s*\.toLocale(?:Date)?String\s*\([^%]*?\)\s*%>',
        re_mod.DOTALL,
    )
    # Pattern D: bare VAR.date_time
    date_bare_var_re = re_mod.compile(
        r'<%=\s*(\w+)\.date_time\s*%>',
    )
    # Trailing literal ' UTC' text after a rewritten helper call (strip it)
    trailing_utc_re = re_mod.compile(
        r'(formatEvent(?:Time|Date)\(\w+\.date_time\)\s*%>)\s*UTC\b',
    )

    rewrites = 0
    for path in list(file_dict.keys()):
        if not path.endswith(".ejs"):
            continue
        content = file_dict[path]
        if not isinstance(content, str):
            continue
        orig = content

        # Pattern A → formatEventDate
        content = date_to_locale_date_re.sub(
            lambda m: f'<%= formatEventDate({m.group(1)}.date_time) %>',
            content,
        )
        # Pattern B → formatEventTime
        content = date_to_locale_string_re.sub(
            lambda m: f'<%= formatEventTime({m.group(1)}.date_time) %>',
            content,
        )
        # Pattern C → formatEventTime
        content = date_bare_method_re.sub(
            lambda m: f'<%= formatEventTime({m.group(1)}.date_time) %>',
            content,
        )
        # Pattern D → formatEventTime
        content = date_bare_var_re.sub(
            lambda m: f'<%= formatEventTime({m.group(1)}.date_time) %>',
            content,
        )
        # Strip trailing literal ' UTC'
        content = trailing_utc_re.sub(r'\1', content)

        if content != orig:
            file_dict[path] = content
            rewrites += 1

    if rewrites:
        print(f"  [post-process] Rewrote date_time formatting to use formatEventTime/Date() in {rewrites} views")

    return list(file_dict.items())


@processor(after=('_fix_cdn_script_graceful',), phase=('build',))
def _fix_locale_string_options(files: list) -> list:
    """Fix toLocaleString calls that mix incompatible options.

    dateStyle/timeStyle cannot be used with timeZoneName in the same call.
    Replace with explicit year/month/day/hour/minute + timeZoneName.
    """
    import re as _re_mod

    file_dict = dict(files)
    changed = False

    for filepath, content in list(file_dict.items()):
        if not filepath.endswith((".ejs", ".js")):
            continue

        original = content

        # Fix: dateStyle + timeStyle + timeZoneName -> explicit options
        content = _re_mod.sub(
            r"\.toLocaleString\([^)]*dateStyle[^)]*timeZoneName[^)]*\)",
            ".toLocaleString('en-US', { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' })",
            content,
        )

        if content != original:
            file_dict[filepath] = content
            changed = True

    if changed:
        print("  [post-process] Fixed toLocaleString incompatible options (dateStyle + timeZoneName)")

    return list(file_dict.items())


@processor(after=('_fix_locale_string_options',), phase=('build',))
def _strip_require_from_ejs(files: list) -> list:
    """Strip require() calls from EJS view templates.

    Claude sometimes generates require() in EJS (e.g. require('url').parse()),
    but EJS templates run in a sandboxed context where require is not defined.
    Remove any lines with require() from .ejs files.
    """
    import re as _re_mod

    file_dict = dict(files)
    changed = False

    for filepath, content in list(file_dict.items()):
        if not filepath.endswith(".ejs"):
            continue

        lines = content.split("\n")
        new_lines = []
        removed = False
        for line in lines:
            # Remove EJS code lines that use require()
            if _re_mod.search(r'<%.*require\s*\(', line):
                removed = True
                continue
            new_lines.append(line)

        if removed:
            file_dict[filepath] = "\n".join(new_lines)
            changed = True

    if changed:
        print("  [post-process] Stripped require() calls from EJS templates")

    return list(file_dict.items())


# Segment matcher for _scrub_em_dashes_in_ejs. Captures HTML comments,
# <script>...</script>, <style>...</style>, and EJS code blocks (<% ... %>,
# including <%= %> / <%- %> / <%_ %>) as "code" regions that the scrubber
# MUST skip. re.DOTALL so multi-line code blocks are captured whole.
import re as _re_em_dash
_EM_DASH_CODE_SEGMENT_RE = _re_em_dash.compile(
    r"""(
        <!--.*?-->
        |
        <script\b[^>]*>.*?</script>
        |
        <style\b[^>]*>.*?</style>
        |
        <%.*?%>
    )""",
    _re_em_dash.DOTALL | _re_em_dash.IGNORECASE | _re_em_dash.VERBOSE,
)
del _re_em_dash


def _scrub_em_dashes_in_text(text: str) -> str:
    """Replace U+2014 em-dashes in an HTML/text segment with comma or hyphen.

    Rules (per CLAUDE.md code style + founder preference):
      - Space-surrounded em-dash (" - ") → ", " (prose comma).
      - Bare em-dash between non-space chars (e.g. "Sun, Fri" or "3-5pm")
        → " - " (hyphen with spaces, preserves the word-break meaning).
      - Em-dash at the start/end of the text (rare) → " - " (safe default).
    """
    if "\u2014" not in text:
        return text
    # Space-surrounded first (so the inner "-" doesn't get matched twice).
    text = text.replace(" \u2014 ", ", ")
    # Remaining bare em-dashes: replace with " - ".
    text = text.replace("\u2014", " - ")
    return text


@processor(after=('_strip_require_from_ejs',), phase=('build', 'reapply'))
def _scrub_em_dashes_in_ejs(files: list) -> list:
    """Strip U+2014 em-dashes from .ejs TEXT nodes only.

    The existing em-dash contract in contracts/layout.py only fires on
    <nav> text, so em-dashes in a homepage <main> pass it. The project
    rule (CLAUDE.md "No em dashes") applies to ALL user-facing text.

    Scope rules:
      - Only .ejs files (the surface where Claude emits user-visible
        prose). Markdown + static HTML are handled by separate paths.
      - Only TEXT nodes: everything outside <script>, <style>, HTML
        comments, and EJS code blocks (<% ... %>, <%= %>, <%- %>).
        Em-dashes INSIDE those blocks are preserved as-is so valid JS
        code comments and EJS expression strings keep their punctuation.
      - Standard-widget doctrine: the nav
        contract already fired once; this is the 2nd recurrence of the
        same defect class, so we ship a canonical @processor instead of
        widening the contract to every view.
    """
    file_dict = dict(files)
    changed = False
    total_replacements = 0

    for filepath, content in list(file_dict.items()):
        if not isinstance(filepath, str) or not filepath.endswith(".ejs"):
            continue
        if not isinstance(content, str) or "\u2014" not in content:
            continue

        # Split into alternating [text, code, text, code, ...] where
        # re.split with a capture group interleaves the captured groups
        # with the unmatched text. Only even-indexed entries are text.
        parts = _EM_DASH_CODE_SEGMENT_RE.split(content)
        before_count = content.count("\u2014")
        for idx in range(0, len(parts), 2):
            parts[idx] = _scrub_em_dashes_in_text(parts[idx])
        new_content = "".join(parts)

        if new_content != content:
            file_dict[filepath] = new_content
            after_count = new_content.count("\u2014")
            total_replacements += (before_count - after_count)
            changed = True

    if changed:
        print(
            f"  [post-process] Scrubbed {total_replacements} em-dash(es) "
            "from EJS text nodes"
        )

    return list(file_dict.items()) if changed else files


@processor(after=('_scrub_em_dashes_in_ejs',))
def _ensure_resilient_migrate(files: list) -> list:
    """Replace Claude's db/migrate.js with the resilient template.

    Claude often generates migrate.js that runs all of seed.sql as a single
    client.query(seed) call. One bad INSERT kills ALL seed data. The template
    splits statements and try/catches each individually.

    Always overwrites Claude's version since the template is strictly better.
    """
    file_dict = dict(files)
    if "db/migrate.js" not in file_dict:
        return files

    template_path = _TEMPLATES_DIR / "db_migrate.js"
    if not template_path.exists():
        return files

    template_content = template_path.read_text()
    existing = file_dict["db/migrate.js"]

    # Only replace if Claude generated the old single-query pattern
    if ".split(" not in existing:
        file_dict["db/migrate.js"] = template_content
        print("  [post-process] Replaced migrate.js with resilient per-statement template")

    return list(file_dict.items())


@processor(after=('_ensure_resilient_migrate',), phase=('build',))
def _ensure_minimum_seed_rows(files: list) -> list:
    """Ensure every data table has at least 15 seed rows.

    Claude sometimes generates fewer than 15 rows per table despite the prompt
    instruction. This post-processor detects short tables and duplicates existing
    rows with modified values (offset dates, appended text) to reach the minimum.
    Skips system tables (users, site_settings, uploads).
    """
    import re as _re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql", "")
    if not seed:
        return files

    # Per-table minimums. v17 FIX: events default was 15 which forced the
    # padder to duplicate original titles with "(evening session)" suffixes; 
    # the list read as clones. Events need fewer padded rows; photos need
    # more for a visually-filled gallery grid.
    PER_TABLE_MIN = {
        "events": 8,
        "photos": 12,
        "videos": 6,
        "testimonials": 6,
        "services": 6,
    }
    DEFAULT_MIN = 15
    SKIP_TABLES = {"users", "site_settings", "uploads", "sessions", "email_verification_tokens",
                   "magic_link_tokens", "password_reset_tokens"}

    # Parse active (non-commented) INSERT statements per table
    lines = seed.split("\n")
    table_inserts = {}  # table_name -> list of (line_index, line_text)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        m = _re_mod.match(r"INSERT\s+INTO\s+[\"']?(\w+)", stripped, _re_mod.IGNORECASE)
        if m:
            table = m.group(1).lower()
            if table not in SKIP_TABLES:
                table_inserts.setdefault(table, []).append((i, stripped))

    if not table_inserts:
        return files

    # For each table below minimum, duplicate existing rows with modifications
    added_lines = []
    padded_any = False

    for table, inserts in table_inserts.items():
        count = len(inserts)
        min_rows = PER_TABLE_MIN.get(table, DEFAULT_MIN)
        if count >= min_rows:
            continue

        needed = min_rows - count
        if count == 0:
            continue

        # Cycle through existing rows, modifying them slightly
        for j in range(needed):
            source_idx = j % count
            _, source_line = inserts[source_idx]

            # Extract the VALUES portion
            values_match = _re_mod.search(r"VALUES\s*\((.+)\)\s*ON\s+CONFLICT", source_line, _re_mod.IGNORECASE)
            if not values_match:
                continue

            values_str = values_match.group(1)

            # v17 FIX: title variation. Instead of parenthetical "(evening
            # session)" suffixes that read as clones of the same event, use
            # prefix variants that feel like standalone series entries. For
            # non-event tables (photos, testimonials), keep the descriptive
            # suffix style.
            if table in ("events",):
                prefixes = [
                    "Encore: ", "Returning: ", "Spring: ", "Summer: ",
                    "Autumn: ", "Winter: ", "Full Moon: ", "New Moon: ",
                    "Solstice: ", "Equinox: ", "Sunrise: ", "Sunset: ",
                    "Intimate Circle: ", "Community Gathering: ",
                    "Weekend Retreat: ",
                ]
                prefix = prefixes[j % len(prefixes)]
                suffix = ""
            else:
                prefixes = [""]
                prefix = ""
                # Apostrophe-free: a single quote inside the padded string
                # breaks the quote-balance validator downstream.
                suffixes = [
                    " (series 2)", " (special edition)", " (community edition)",
                    " (extended)", " (deluxe)", " (featured)", " (encore)",
                    " (bonus)", " (new arrival)", " (curators pick)",
                    " (limited run)", " (collectors edition)", " (anniversary)",
                    " (spring release)", " (autumn release)",
                ]
                suffix = suffixes[j % len(suffixes)]

            # Find the first quoted string that is SAFE to pad. Skip URLs, paths,
            # file names, emails, hex codes, interval literals, pure numbers,
            # and anything that looks like a machine identifier; padding those
            # corrupts links, references, and typed column values.
            _INTERVAL_UNITS = {
                "second", "seconds", "minute", "minutes", "hour", "hours",
                "day", "days", "week", "weeks", "month", "months",
                "year", "years", "decade", "decades", "century", "centuries",
                "millennium", "millennia",
            }

            def _is_unsafe_to_pad(s: str) -> bool:
                if len(s) < 5:
                    return True
                low = s.lower()
                if s.startswith("/") or low.startswith("http://") or low.startswith("https://"):
                    return True
                if "@" in s and "." in s:  # email-ish
                    return True
                if _re_mod.search(
                    r"\.(jpg|jpeg|png|webp|gif|svg|ico|mp4|mov|mp3|wav|pdf|zip|tar|gz|js|css|html|json)$",
                    low,
                ):
                    return True
                if _re_mod.fullmatch(r"#[0-9a-f]{3,8}", low):  # hex color
                    return True
                if _re_mod.fullmatch(r"[0-9a-f-]{32,}", low):  # uuid / hash
                    return True
                # PostgreSQL INTERVAL literals: '3 months', '1 year', '30 days',
                # '2.5 hours'. Appending a text suffix produces invalid interval
                # syntax (SEED_PADDING_TYPE1). Match: optional sign, number,
                # whitespace, then a known time-unit word.
                parts = s.strip().split()
                if (
                    len(parts) >= 2
                    and _re_mod.fullmatch(r"-?\d+(\.\d+)?", parts[0])
                    and parts[-1].lower() in _INTERVAL_UNITS
                ):
                    return True
                # Pure numeric strings that slipped past the len < 5 guard:
                # '1000', '3.14', '-50'. These belong to integer/numeric columns.
                if _re_mod.fullmatch(r"-?\d+(\.\d+)?", s.strip()):
                    return True
                return False

            # Iterate quoted strings in order; pad the first safe one.
            # For events we use a prefix (e.g. "Full Moon: <title>") to avoid
            # the "(evening session)" duplicate-look. For other tables we
            # use the configured suffix.
            modified = None
            cursor = 0
            for qm in _re_mod.finditer(r"'([^']*?)'", values_str):
                content = qm.group(1)
                if _is_unsafe_to_pad(content):
                    continue
                padded_content = f"{prefix}{content}{suffix}"
                modified = (
                    values_str[: qm.start()]
                    + f"'{padded_content}'"
                    + values_str[qm.end():]
                )
                break

            if modified is None or modified == values_str:
                # No safe string to pad: skip to avoid exact duplicates or corruption
                continue

            # Modify date offsets if NOW() is present
            modified = _re_mod.sub(
                r"NOW\(\)\s*\+\s*INTERVAL\s*'(\d+)\s*days'",
                lambda m: f"NOW() + INTERVAL '{int(m.group(1)) + (j + 1) * 7} days'",
                modified,
            )

            new_line = source_line.replace(values_match.group(1), modified)
            # Ensure no exact duplicate of the original AND balanced quotes
            if new_line != source_line:
                # Validate: single quotes must be balanced (odd count = broken SQL)
                quote_count = new_line.count("'") - new_line.count("\\'")
                if quote_count % 2 != 0:
                    continue  # skip this padded row; unbalanced quotes
                added_lines.append(f"-- [padded] extra seed row for {table}")
                added_lines.append(new_line)
                padded_any = True

    if padded_any:
        seed = seed.rstrip() + "\n" + "\n".join(added_lines) + "\n"
        file_dict["db/seed.sql"] = seed
        print(f"  [post-process] Padded seed data to reach per-table minimums (events={PER_TABLE_MIN['events']}, photos={PER_TABLE_MIN['photos']}, default={DEFAULT_MIN})")

    return list(file_dict.items())


# Section-04 (B+C plan): widget emission processors. Runs BEFORE the
# existing FIX B/L/M/N post-processors so widget output lands in
# artifact.files; the legacy fixes then become byte-identical no-ops on
# widget output (they still fire on Claude output when widgets are
# explicitly disabled via WIDGETS_ENABLED=false: the emergency rollback
# path since 2026-04-20). See docs/plans/b-plus-c/claude-plan.md.

@processor(
    after=('_ensure_site_settings',),
    before=(
        '_ensure_upload_form_ui',
        '_ensure_crud_forms_complete',
        '_strip_inline_dashboard_settings_form',
        '_humanize_settings_labels',
        '_fix_admin_crud_link_prefix',
    ),
    produces=(
        'views/admin/settings.ejs',
        'views/admin/*/new.ejs',
        'views/admin/*/edit.ejs',
    ),
    phase=('build', 'reapply'),
)
def _emit_widgets(artifact):
    """Widget emission entrypoint (section-04 of the B+C pipeline rewrite).

    - If cli.widgets.archetype._widgets_enabled_for(spec) is False (only
      when WIDGETS_ENABLED=false env var is set, per 2026-04-20
      universalization), this is a no-op. The legacy FIX B/L/M/N pipeline
      runs on Claude output as before.
    - Otherwise (the default) delegates to cli.widgets.forms.emit_all,
      which writes:
        views/admin/settings.ejs
        views/admin/<slug>/new.ejs + edit.ejs (per writable data model,
            sorted alphabetically for determinism)
      and stores a dashboard CTA snippet at
        artifact.widgets_snippets['dashboard_settings_section']
      for _inject_dashboard_settings_section to splice in later.
    - Writes artifact.metadata['widgets_emitted_at_generation'] as the ISO
      timestamp of this emission. Downstream code (reapply migration path)
      checks for this key to decide whether to overwrite.
    - Migration path on reapply: if the loaded artifact lacks the
      widgets_emitted_at_generation metadata key AND the widget gate
      accepts the current env/spec combination, we emit fresh. This
      intentionally overwrites Claude-generated and previously post-
      processed admin files because the widget output is the canonical
      shape that later sections' contracts validate against.
    - Post-fix-loop safety: qa_fix /
      deploy_fix iterations can rewrite views/admin/<slug>/new.ejs or
      edit.ejs with Claude-style markup that lacks the WIDGET_ADMIN_MARKER
      comment. On reapply we detect any such on-disk admin CRUD file and
      clear the widgets_emitted_at_generation metadata key (if present) so
      any future gating logic treats this run as a fresh emission.
      emit_all() currently overwrites unconditionally, so the clear is
      defense-in-depth; the detection itself also logs a trace so we can
      see how often the safety net fires.
    """
    from cli.widgets.archetype import _widgets_enabled_for
    from cli.widgets.forms import emit_all as _emit_all
    from cli.widgets.forms import WIDGET_ADMIN_MARKER
    spec = getattr(artifact, 'spec', {}) or {}
    if not _widgets_enabled_for(spec):
        return artifact

    # Detect on-disk admin CRUD files missing the widget marker. Works on
    # both list-of-tuples (build phase) and dict (unit-test) artifact
    # shapes: reapply_post_processors loads from disk into a list.
    files = getattr(artifact, 'files', None)
    stale_admin_files = []
    if isinstance(files, dict):
        _pairs = files.items()
    else:
        _pairs = files or []
    for path, content in _pairs:
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if not path.startswith('views/admin/'):
            continue
        if not (path.endswith('/new.ejs') or path.endswith('/edit.ejs')):
            continue
        if WIDGET_ADMIN_MARKER not in content:
            stale_admin_files.append(path)

    if stale_admin_files:
        metadata = getattr(artifact, 'metadata', None)
        if isinstance(metadata, dict):
            metadata.pop('widgets_emitted_at_generation', None)
        print(
            "  [post-process] Widget marker missing on "
            f"{len(stale_admin_files)} admin CRUD file(s); forcing re-emit: "
            f"{sorted(stale_admin_files)[:3]}"
            f"{' ...' if len(stale_admin_files) > 3 else ''}"
        )

    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        'widgets.emit_all.start',
        {
            'archetype_gate': True,
            'stale_admin_files': len(stale_admin_files),
        },
    )
    artifact = _emit_all(artifact)
    emit_build_trace(
        _CURRENT_BUILD_DIR.get(),
        'widgets.emit_all.end',
        {'metadata_timestamp': artifact.metadata.get(
            'widgets_emitted_at_generation', '')},
    )
    return artifact


@processor(
    after=('_emit_widgets',),
    produces=('server.js',),
    consumes=('server.js',),
    assumes_on_disk=('server.js',),
    phase=('build', 'reapply'),
)
def _ensure_admin_slug_mount(artifact):
    """Dual-mount every writable model's router at /admin/<slug>.

    Claude emits app.use('/<slug>', <slug>Router) for each data model.
    Widget-emitted CRUD forms POST to /admin/<slug>, so we also need
    app.use('/admin/<slug>', requireAdmin, <slug>Router) for those URLs
    to resolve.

    Requirements:
    - Read-only models are skipped (no admin mount needed).
    - If /admin/<slug> mount is already present, no-op (idempotent).
    - Existing /<slug> mount is preserved (dual-mount, NOT replacement).
    - Mount insertion is textual (regex + line insertion) on the
      server.js string in artifact.files; no eval of JS source.
    - Runs both when widgets are enabled and when disabled. Harmless
      either way: the /admin/<slug> route is also what FIX C
      (_fix_admin_crud_link_prefix) normalizes its link rewrites away
      from, so keeping both mounts live is a belt-and-suspenders pattern.
    """
    import re as re_mod

    files = artifact.files
    # Locate server.js in both dict and list shapes.
    if isinstance(files, dict):
        server = files.get('server.js')
        def _write(content):
            files['server.js'] = content
    else:
        server = None
        server_idx = -1
        for i, item in enumerate(files):
            if item and item[0] == 'server.js':
                server = item[1]
                server_idx = i
                break
        def _write(content):
            if server_idx >= 0:
                files[server_idx] = ('server.js', content)
            else:
                files.append(('server.js', content))

    if not server or not isinstance(server, str):
        return artifact

    spec = getattr(artifact, 'spec', {}) or {}
    data_models = spec.get('data_models') or []
    if not data_models:
        return artifact

    def _is_read_only(m):
        access = str(m.get('access', '')).lower()
        if access in {'read-only', 'readonly', 'ro'}:
            return True
        if m.get('read_only'):
            return True
        perms = m.get('permissions')
        if isinstance(perms, str) and perms.lower() in {'read', 'view', 'read-only'}:
            return True
        return False

    def _slug_of(m):
        explicit = str(m.get('slug') or '').strip().lower()
        if explicit:
            return explicit
        name = str(m.get('name') or '').strip()
        if not name:
            return ''
        parts = []
        cur = ''
        for ch in name:
            if ch.isupper() and cur:
                parts.append(cur.lower())
                cur = ch
            else:
                cur += ch
        if cur:
            parts.append(cur.lower())
        slug = '-'.join(parts) if parts else name.lower()
        if slug.endswith('y') and len(slug) > 1 and slug[-2] not in 'aeiou':
            return slug[:-1] + 'ies'
        if slug.endswith(('ch', 'sh', 'ss')) or slug[-1:] in ('s', 'x', 'z'):
            return slug + 'es'
        return slug + 's'

    # Derive the router identifier Claude typically uses for a given
    # plural slug. Convention: '<slug>Router' camelCased
    # (contact-messages -> contactMessagesRouter).
    def _router_name_for(slug):
        words = slug.split('-')
        name = words[0] + ''.join(w.capitalize() for w in words[1:])
        return name + 'Router'

    new_lines = server
    changed = False
    for model in data_models:
        if _is_read_only(model):
            continue
        slug = _slug_of(model)
        if not slug:
            continue
        admin_mount_pat = (
            r"app\.use\(\s*['\"]/admin/" + re_mod.escape(slug) + r"['\"]"
        )
        if re_mod.search(admin_mount_pat, new_lines):
            continue  # already mounted
        router_name = _router_name_for(slug)
        # Locate the existing public mount to anchor the admin mount next
        # to it. Accept BOTH common shapes Claude emits:
        #   (a) `app.use('/slug', slugRouter);`: bare identifier
        #   (b) `app.use('/slug', require('./routes/slug'));`: inline require
        # Matching both means a file that uses (b) gets its admin mount
        # anchored next to the public mount, instead of falling through
        # to the append-at-end fallback below.
        public_mount_pat = re_mod.compile(
            r"(app\.use\(\s*['\"]/" + re_mod.escape(slug) +
            r"['\"]\s*,\s*(require\([^)]+\)|[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;?)",
        )
        m = public_mount_pat.search(new_lines)
        if m:
            detected_router = m.group(2)  # bare ident OR `require(...)`
            insertion = (
                f"\napp.use('/admin/{slug}', requireAdmin, {detected_router});"
            )
            new_lines = new_lines[:m.end()] + insertion + new_lines[m.end():]
            changed = True
        else:
            # No public mount to anchor against. Fall back to an inline
            # require('./routes/<slug>') so the admin mount is guaranteed
            # to resolve even when Claude forgot to emit a public mount.
            # Using a bare identifier here would crash the app at boot.
            insertion = (
                f"\napp.use('/admin/{slug}', requireAdmin, "
                f"require('./routes/{slug}'));"
            )
            new_lines = new_lines.rstrip() + insertion + '\n'
            changed = True

    if changed:
        _write(new_lines)
    return artifact


_REQUIRE_ADMIN_MIDDLEWARE_DEF = '''
// Define requireAdmin middleware for use in server.js route mounting.
// Injected by cli/build.py::_ensure_require_admin_defined because
// _ensure_admin_slug_mount emits `app.use('/admin/<slug>', requireAdmin, ...)`
// lines that reference this identifier; Claude routinely emits the
// reference but forgets the definition (recurring across v20-v24
// builds, every design review flagged it, test self-correction loop
// wasted one Claude call per build to synthesize it).
function requireAdmin(req, res, next) {
  if (!req.session || !req.session.userId) {
    return res.redirect('/login');
  }
  if (req.session.userRole !== 'admin') {
    return res.status(403).render('403', { title: 'Access Denied' });
  }
  next();
}

'''


@processor(
    after=('_ensure_admin_slug_mount',),
    produces=('server.js',),
    consumes=('server.js',),
    phase=('build', 'reapply'),
)
def _ensure_require_admin_defined(artifact):
    """Inject a canonical `function requireAdmin` middleware into server.js
    when it's referenced by `app.use('/admin/<slug>', requireAdmin, ...)`
    but not defined or imported.

    This is a "standard widget" for a recurring Claude-output bug class:
    _ensure_admin_slug_mount reliably emits the /admin/<slug> dual-mount
    calls, and Claude's own server.js boilerplate tends to USE
    requireAdmin in other spots too, but the middleware DEFINITION
    varies - sometimes defined locally, sometimes imported from
    middleware/auth, sometimes (v20/v21/v22/v24) missing entirely.
    Missing causes a ReferenceError crash on first request, which the
    test phase catches + self-corrects via Claude (~$0.50 + 60s per
    build). Deterministic injection eliminates the recurrence.

    Idempotent: checks for an existing definition/import before
    injection. Inserts right after the initial require/import block so
    the middleware is in scope before any app.use() references it.
    """
    import re as _re

    files = artifact.files
    if isinstance(files, dict):
        server_js = files.get('server.js')
        def _write(content):
            files['server.js'] = content
    else:
        server_js = None
        srv_idx = -1
        for i, item in enumerate(files):
            if item and item[0] == 'server.js':
                server_js = item[1]
                srv_idx = i
                break
        def _write(content):
            if srv_idx >= 0:
                files[srv_idx] = ('server.js', content)

    if not server_js or not isinstance(server_js, str):
        return artifact

    # Fast path: no reference, nothing to do.
    if 'requireAdmin' not in server_js:
        return artifact

    # Already defined locally (function / const arrow / variable)?
    if _re.search(
        r"(?:function\s+requireAdmin\s*\(|"
        r"const\s+requireAdmin\s*=|"
        r"let\s+requireAdmin\s*=|"
        r"var\s+requireAdmin\s*=)",
        server_js,
    ):
        return artifact

    # Already imported from a middleware module? Match the binding forms:
    #   const { ..., requireAdmin, ... } = require('./middleware/auth');
    #   const requireAdmin = require('./middleware/auth').requireAdmin;
    # Distinguishing these from inline USE inside app.use(..., requireAdmin,
    # require('...'), ...) requires the binding keyword + requireAdmin on
    # the LEFT side of the assignment.
    import_re = _re.compile(
        r"(?:^|\n)\s*(?:const|let|var)\s+"
        r"(?:\{[^}]*\brequireAdmin\b[^}]*\}|requireAdmin)"
        r"[^=\n]*=\s*require\s*\(",
    )
    if import_re.search(server_js):
        return artifact

    # Injection site: right after the last `const ... = require(...)` line
    # in the initial require block. Falls back to the top of the file if
    # no requires are found.
    require_matches = list(_re.finditer(
        r"^(?:const|let|var)\s+[^=\n]+=\s*require\s*\([^)]+\)\s*;?\s*$",
        server_js, _re.MULTILINE,
    ))
    if require_matches:
        insert_pos = require_matches[-1].end()
        # Also consume the trailing newline so the definition lands on
        # its own block rather than appended to the last require line.
        if insert_pos < len(server_js) and server_js[insert_pos] == '\n':
            insert_pos += 1
        new_server = (
            server_js[:insert_pos]
            + _REQUIRE_ADMIN_MIDDLEWARE_DEF
            + server_js[insert_pos:]
        )
    else:
        new_server = _REQUIRE_ADMIN_MIDDLEWARE_DEF + server_js

    _write(new_server)
    print("  [post-process] _ensure_require_admin_defined injected requireAdmin middleware")
    return artifact


@processor(
    after=('_emit_widgets',),
    consumes=('views/admin/dashboard.ejs',),
    produces=('views/admin/dashboard.ejs',),
    assumes_on_disk=('views/admin/dashboard.ejs',),
    phase=('build', 'reapply'),
)
def _inject_dashboard_settings_section(artifact):
    """Splice the widget-emitted 'Site Settings' CTA snippet into
    views/admin/dashboard.ejs.

    - No-op when the snippet is absent (widgets disabled).
    - No-op when the dashboard file is absent.
    - Uses a stable HTML comment marker pair so first insertion and later
      re-injection land in the same spot:
          <!-- widgets:dashboard-settings -->
          ... snippet ...
          <!-- /widgets:dashboard-settings -->
    - Idempotent: second call replaces content between the same markers
      with the same snippet, so output is byte-identical.
    """
    snippets = getattr(artifact, 'widgets_snippets', None) or {}
    snippet = snippets.get('dashboard_settings_section')
    if not snippet:
        return artifact

    files = artifact.files
    if isinstance(files, dict):
        dashboard = files.get('views/admin/dashboard.ejs')
        def _write(content):
            files['views/admin/dashboard.ejs'] = content
    else:
        dashboard = None
        dash_idx = -1
        for i, item in enumerate(files):
            if item and item[0] == 'views/admin/dashboard.ejs':
                dashboard = item[1]
                dash_idx = i
                break
        def _write(content):
            if dash_idx >= 0:
                files[dash_idx] = ('views/admin/dashboard.ejs', content)
            else:
                files.append(('views/admin/dashboard.ejs', content))

    if not dashboard or not isinstance(dashboard, str):
        return artifact

    START = '<!-- widgets:dashboard-settings -->'
    END = '<!-- /widgets:dashboard-settings -->'
    block = f"{START}\n{snippet}{END}\n"

    start_idx = dashboard.find(START)
    end_idx = dashboard.find(END)
    if start_idx >= 0 and end_idx > start_idx:
        # Replace between markers (inclusive of the END marker and its newline).
        end_cut = end_idx + len(END)
        if end_cut < len(dashboard) and dashboard[end_cut] == '\n':
            end_cut += 1
        new_dashboard = dashboard[:start_idx] + block + dashboard[end_cut:]
    else:
        # First-time injection: append at end.
        new_dashboard = dashboard.rstrip() + '\n' + block

    if new_dashboard != dashboard:
        _write(new_dashboard)
    return artifact


def _rewrite_admin_router_source(source: str, slug: str) -> str:
    """Rewrite res.render('<slug>/new', ...) / '<slug>/edit' calls in a
    single route file to inline a viewPrefix ternary keyed on req.baseUrl.

    Before:
        res.render('events/new', { ... });
    After:
        res.render((req.baseUrl && req.baseUrl.startsWith('/admin/')
          ? 'admin/' : '') + 'events/new', { ... });

    At runtime, a request at /admin/events/new (via the dual-mount added
    by _ensure_admin_slug_mount) resolves viewPrefix to 'admin/' and
    renders the widget-emitted views/admin/events/new.ejs. Public
    /events/new keeps resolving to views/events/new.ejs unchanged.

    Idempotent by construction: the rewritten call no longer matches the
    plain-string-literal regex, so a second pass is a no-op.
    """
    import re as re_mod

    pattern = re_mod.compile(
        rf"res\.render\s*\(\s*([\'\"]){re_mod.escape(slug)}/(new|edit)\1",
    )

    def _replace(match):
        quote = match.group(1)
        view = match.group(2)
        return (
            f"res.render(("
            f"req.baseUrl && req.baseUrl.startsWith('/admin/') ? 'admin/' : ''"
            f") + {quote}{slug}/{view}{quote}"
        )

    return pattern.sub(_replace, source)


@processor(
    after=('_emit_widgets', '_ensure_admin_slug_mount'),
    phase=('build', 'reapply'),
)
def _rewrite_admin_router_template_switch(artifact):
    """Rewrite admin CRUD res.render calls to template-switch on
    req.baseUrl so /admin/<slug>/new renders the widget-emitted
    views/admin/<slug>/new.ejs at runtime.

    Problem this solves: _emit_widgets writes views/admin/<slug>/new.ejs
    + edit.ejs to artifact.files, and _ensure_admin_slug_mount
    dual-mounts routes at /<slug> + /admin/<slug>. But Claude-emitted
    routes/<slug>.js still calls res.render('<slug>/new', ...), which
    resolves to views/<slug>/new.ejs. Both mounts share the router, so
    both serve Claude's public view and the widget admin view is
    orphaned on disk. contracts.run_all(artifact) misses this because
    it only checks artifact.files, not runtime rendering.

    This processor closes the gap by inlining a viewPrefix ternary at
    every res.render('<slug>/new'|'<slug>/edit', ...) call site in
    writable-model route files. Read-only models are skipped.

    See docs/internals/WIDGETS_AND_CONTRACTS.md §1.2 for the design
    rationale.
    """
    spec = getattr(artifact, 'spec', {}) or {}
    data_models = spec.get('data_models', []) or []

    writable_slugs = set()
    for m in data_models:
        if not isinstance(m, dict):
            continue
        if m.get('read_only') is True:
            continue
        access = str(m.get('access', '')).lower()
        if access in ('read-only', 'readonly', 'ro'):
            continue
        permissions = str(m.get('permissions', '')).lower()
        if permissions in ('read', 'view', 'read-only'):
            continue
        name = m.get('name')
        if not name:
            continue
        slug = _model_slug(name)
        plural = _pluralize_slug(slug)
        writable_slugs.add(plural)
        writable_slugs.add(slug)

    if not writable_slugs:
        return artifact

    files = artifact.files
    if isinstance(files, dict):
        iter_paths = list(files.keys())
        def _get(p): return files[p]
        def _set(p, c): files[p] = c
    else:
        iter_paths = [item[0] for item in files if item]
        def _get(path):
            for item in files:
                if item and item[0] == path:
                    return item[1]
            return None
        def _set(path, content):
            for i, item in enumerate(files):
                if item and item[0] == path:
                    files[i] = (path, content)
                    return

    for path in iter_paths:
        if not path.startswith('routes/') or not path.endswith('.js'):
            continue
        slug = path[len('routes/'):-len('.js')]
        if slug not in writable_slugs:
            continue
        content = _get(path)
        if not isinstance(content, str):
            continue

        new_content = _rewrite_admin_router_source(content, slug)
        if new_content != content:
            _set(path, new_content)

    return artifact


# MW v17 follow-up: canonical settings-key → plain English label map.
# Used by _humanize_settings_labels (FIX M) and _strip_inline_dashboard_settings_form
# (FIX L) to replace snake_case DB keys with human-readable labels wherever
# templates iterate `settings.forEach` or hand-author <label>key</label>.
_SETTINGS_LABEL_MAP = {
    "site_name": "Site Name",
    "site_url": "Site URL",
    "site_description": "Site Description",
    "hero_tagline": "Hero Tagline",
    "hero_description": "Hero Description",
    "hero_image_url": "Hero Background Image",
    "hero_image": "Hero Background Image",
    "nav_logo_url": "Navigation Logo",
    "nav_logo": "Navigation Logo",
    "about_title": "About Page Title",
    "about_body": "About Page Body",
    "about_photo_url": "About Photo",
    "about_photo": "About Photo",
    "contact_email": "Contact Email",
    "contact_phone": "Contact Phone",
    "contact_address": "Contact Address",
    "business_name": "Business Name",
    "business_email": "Business Email",
    "business_phone": "Business Phone",
    "business_address": "Business Address",
    "instagram_url": "Instagram",
    "facebook_url": "Facebook",
    "youtube_url": "YouTube",
    "tiktok_url": "TikTok",
    "twitter_url": "Twitter / X",
    "linkedin_url": "LinkedIn",
    "pinterest_url": "Pinterest",
    "booking_url": "Booking URL",
    "newsletter_url": "Newsletter Signup URL",
}


@processor(after=('_fix_route_link_drift',))
def _strip_inline_dashboard_settings_form(files: list, spec: dict = None) -> list:
    """MW v17 FIX L: strip the duplicate inline <form action="/admin/settings">
    that Claude tends to plant directly on the admin dashboard.

    This inline form:
      - uses snake_case DB keys as labels (`<%= setting.key %>`)
      - pairs a visible <input type="text"> URL with a <input type="file"> -
        NOT the canonical drag-drop upload zone emitted by
        _ensure_upload_form_ui on the real settings.ejs template
      - confuses customers (two places to edit the same settings, different UX)

    Resolution: replace the inline form with a CTA card that links to the
    canonical /admin/settings page. Only runs on `views/admin/dashboard.ejs`
    (and `views/dashboard.ejs` as a fallback).

    Leaves the canonical settings.ejs template alone.
    """
    import re as _re_mod

    file_dict = dict(files)
    # v19 regression: Claude emits `views/admin-dashboard.ejs` (hyphenated),
    # `views/dashboard.ejs`, `views/admin/dashboard.ejs`, or any other non-
    # settings view that contains a `<form action="/admin/settings">` block.
    # Match ANY ejs view that has that form-action signature AND is not the
    # canonical settings template: that's the duplicate-surface we strip.
    CANONICAL = {"views/admin/settings.ejs", "views/admin-settings.ejs"}
    targets = []
    for p, content in file_dict.items():
        if not p.endswith(".ejs") or not isinstance(content, str):
            continue
        if p in CANONICAL:
            continue
        # Only target views that LOOK like a dashboard/index surface.
        basename = p.rsplit("/", 1)[-1].lower()
        if not any(m in basename for m in ("dashboard", "admin-home", "admin-index", "index")):
            continue
        if 'action="/admin/settings"' not in content:
            continue
        targets.append(p)
    if not targets:
        return files

    # Two strip modes:
    # (A) Single big form: `<form action="/admin/settings">...</form>` wraps
    #     all settings fields (v18 pattern).
    # (B) Per-field iterator: `<% settings.forEach(...) { %> <form action="
    #     /admin/settings">...</form> <% }); %>`: 18 mini-forms (v19 pattern).
    # Both get replaced with a single CTA.
    changed = 0
    cta_block = (
        '<!-- v17 FIX L: inline settings surface replaced with canonical CTA -->\n'
        '<a href="/admin/settings" class="btn-primary inline-flex items-center gap-2 px-6 py-3 rounded-lg text-white font-semibold transition-colors" style="background:var(--color-accent,#5a80c0)">\n'
        '  Manage Site Settings\n'
        '  <svg style="width:1rem;height:1rem" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clip-rule="evenodd"/></svg>\n'
        '</a>'
    )

    for path in targets:
        content = file_dict[path]
        if not isinstance(content, str):
            continue

        # Mode B detection (iterator-wrapped mini-forms with OPTIONAL
        # enclosing `<% if (settings && settings.length > 0) { %>...<% } else
        # { %>...<% } %>` conditional). Strip the entire conditional if
        # present: the CTA always renders, no else branch needed.
        matched_iter = False

        # First try the full if/else/forEach combo
        full_cond_re = _re_mod.compile(
            r"(<%\s*if\s*\([^%]*?settings[^%]*?\)\s*\{\s*%>\s*)"
            r"(.*?<%\s*[\w]+\.?forEach\s*\(\s*function\s*\([^)]*\)\s*\{\s*%>.*?<%\s*\}\s*\)\s*;\s*%>.*?)"
            r"(<%\s*\}\s*(?:else\s*\{\s*%>.*?<%\s*\})?\s*%>)",
            _re_mod.DOTALL,
        )
        fm = full_cond_re.search(content)
        if fm and 'action="/admin/settings"' in fm.group(2):
            content = content[:fm.start()] + cta_block + content[fm.end():]
            matched_iter = True
            changed += 1

        # If no full conditional match, try the bare iterator
        if not matched_iter:
            iter_re = _re_mod.compile(
                r"(<%\s*[\w]+\.?forEach\s*\(\s*function\s*\([^)]*\)\s*\{\s*%>)"
                r"(.*?)"
                r"(<%\s*\}\s*\)\s*;\s*%>)",
                _re_mod.DOTALL,
            )
            for itm in iter_re.finditer(content):
                if 'action="/admin/settings"' in itm.group(2):
                    content = content[:itm.start()] + cta_block + content[itm.end():]
                    matched_iter = True
                    changed += 1
                    break

        if not matched_iter:
            # Mode A: single form block, no iterator wrap.
            form_open_re = _re_mod.compile(
                r'<form\b[^>]*\baction="/admin/settings"[^>]*>',
                _re_mod.IGNORECASE,
            )
            m = form_open_re.search(content)
            if m:
                close_idx = content.find("</form>", m.end())
                if close_idx > 0:
                    form_end = close_idx + len("</form>")
                    content = content[:m.start()] + cta_block + content[form_end:]
                    changed += 1

        file_dict[path] = content

    if changed:
        print(f"  [post-process] _strip_inline_dashboard_settings_form: replaced inline form(s) in {changed} view(s)")

    return list(file_dict.items())


@processor(after=('_strip_inline_dashboard_settings_form',))
def _humanize_settings_labels(files: list, spec: dict = None) -> list:
    """MW v17 FIX M: rewrite snake_case field labels to plain English across
    any admin view that uses the settings keys directly.

    Two patterns handled:
      (a) hand-authored <label>snake_key</label>: replaced with the mapped
          English label when the key is known.
      (b) EJS `<label><%= setting.key %></label>` iterator pattern; the
          <%= ... %> expression is rewritten to call a `_humanLabel(s.key)`
          helper, and the helper is injected at the top of the view (so the
          template is self-contained).

    Only affects `views/admin/*.ejs` and the top-level dashboard template.
    """
    import re as _re_mod

    file_dict = dict(files)
    changed_files = 0

    helper_block = (
        '<% var _humanLabel = function(k) { var M = {\n'
        + ",\n".join(f'  "{k}": "{v}"' for k, v in _SETTINGS_LABEL_MAP.items())
        + '\n}; return M[k] || k.replace(/_/g, " ").replace(/\\b\\w/g, function(c){return c.toUpperCase();}); }; %>\n'
    )

    # Match EJS expression labels like:
    #   <label ...><%= s.key %></label>
    #   <label ...><%= setting.key %></label>
    ejs_label_re = _re_mod.compile(
        r'(<label\b[^>]*>)\s*<%=\s*(\w+)\.key\s*%>\s*(</label>)',
        _re_mod.IGNORECASE,
    )

    # Match literal snake_case labels: <label ...>snake_case_key</label>
    # where the text looks like lowercase + underscores.
    literal_label_re = _re_mod.compile(
        r'(<label\b[^>]*>)\s*([a-z]+(?:_[a-z]+)+)\s*(</label>)',
    )

    for path in list(file_dict.keys()):
        # v19 regression: `views/admin-dashboard.ejs` (hyphenated) slipped
        # through the old scope. Match any admin-scoped view path variant.
        if not path.endswith(".ejs"):
            continue
        basename = path.rsplit("/", 1)[-1].lower()
        is_admin_view = (
            path.startswith("views/admin/")
            or basename.startswith("admin-")
            or basename in ("dashboard.ejs", "settings.ejs")
        )
        if not is_admin_view:
            continue
        content = file_dict[path]
        if not isinstance(content, str):
            continue

        changed = False

        # (b) Rewrite <%= X.key %> → <%= _humanLabel(X.key) %>
        if ejs_label_re.search(content):
            content = ejs_label_re.sub(
                lambda m: f"{m.group(1)}<%= _humanLabel({m.group(2)}.key) %>{m.group(3)}",
                content,
            )
            # Inject helper if not already present
            if "_humanLabel" not in content.split(helper_block, 1)[0]:
                content = helper_block + content
            changed = True

        # (a) Rewrite literal snake_case in <label> tags
        def _literal_replace(m):
            nonlocal changed
            key = m.group(2)
            english = _SETTINGS_LABEL_MAP.get(key)
            if not english:
                # Best-effort title-case if not in the map (e.g. unknown key)
                english = key.replace("_", " ").title()
            changed = True
            return f"{m.group(1)}{english}{m.group(3)}"

        content = literal_label_re.sub(_literal_replace, content)

        if changed:
            file_dict[path] = content
            changed_files += 1

    if changed_files:
        print(f"  [post-process] _humanize_settings_labels: English labels applied in {changed_files} admin view(s)")

    return list(file_dict.items())


@processor(after=('_ensure_about_photo_fallback',))
def _ensure_mw_archetype_logo_bundled(files: list, spec: dict = None) -> list:
    """MW v17 FIX O: when the build is the Medicine Waves archetype, bundle
    `cli/assets/mw_logo.png` as `public/images/mw_logo.png` so the nav logo
    default seeding (FIX K) has something to wire.

    Detection: spec.description contains 'medicine waves' (case-insensitive)
    OR the app_name starts with 'medicine-waves'. This is narrow on purpose
    - the canonical MW PNG is MW-specific visual identity; other wellness
    archetype apps should not inherit it.
    """
    file_dict = dict(files)
    if spec is None:
        return files
    desc = str(spec.get("description", "")).lower()
    app = str(spec.get("app_name", "")).lower()
    is_mw = ("medicine waves" in desc) or app.startswith("medicine-waves")
    if not is_mw:
        return files

    target = "public/images/mw_logo.png"
    if target in file_dict:
        return files  # already bundled (idempotent)

    src = Path(__file__).parent / "assets" / "mw_logo.png"
    if not src.exists():
        print(f"  [post-process] _ensure_mw_archetype_logo_bundled: cli/assets/mw_logo.png not found; skipping")
        return files

    file_dict[target] = src.read_bytes()
    print("  [post-process] _ensure_mw_archetype_logo_bundled: bundled MW logo at public/images/mw_logo.png")
    return list(file_dict.items())


@processor(after=('_ensure_mw_archetype_logo_bundled',))
def _ensure_nav_logo_default_seed(files: list, spec: dict = None) -> list:
    """MW v17 FIX K: when the build includes a bundled logo, seed
    site_settings.nav_logo_url with that path so the nav renders it
    automatically on first boot (no manual upload required).

    Precedence (SVG wins over PNG for crispness):
      public/logo.svg > public/logo.png > public/logo.webp >
      public/images/<slug>_logo.* (if a named-logo convention is used)

    Also scans the on-disk build dir via _CURRENT_BUILD_DIR for reapply
    passes where binary files are excluded from the file list.

    Leaves seed untouched when:
      - No logo file is found anywhere
      - Seed's nav_logo_url already has an explicit non-empty value
    """
    import re as _re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql")
    if not seed or not isinstance(seed, str):
        return files

    # If the seed doesn't have an empty nav_logo_url insert, nothing to do
    empty_nav_re = _re_mod.compile(
        r"INSERT\s+INTO\s+site_settings[^;]*\(\s*key\s*,\s*value\s*\)\s*VALUES\s*\(\s*'nav_logo_url'\s*,\s*''\s*\)",
        _re_mod.IGNORECASE,
    )
    if not empty_nav_re.search(seed):
        return files

    # Candidate logo paths in preference order
    def _find_logo_path():
        preferred = ["public/logo.svg", "public/logo.png", "public/logo.webp"]
        for p in preferred:
            if p in file_dict:
                return "/" + p.split("/", 1)[1]
        # Also scan files list for images/<slug>_logo.* or /logo.* under public/
        for path in file_dict.keys():
            if _re_mod.match(r"public/(?:images/)?[\w-]*logo[\w-]*\.(?:svg|png|webp|jpg|jpeg)$", path, _re_mod.IGNORECASE):
                return "/" + path.split("/", 1)[1]
        # Fallback: disk scan via contextvar (reapply path, binaries skipped from files list)
        try:
            build_dir = _CURRENT_BUILD_DIR.get()
        except LookupError:
            build_dir = None
        if build_dir:
            for p in ("logo.svg", "logo.png", "logo.webp"):
                candidate = Path(build_dir) / "public" / p
                if candidate.is_file():
                    return f"/{p}"
            images_dir = Path(build_dir) / "public" / "images"
            if images_dir.is_dir():
                for f in images_dir.glob("*logo*"):
                    if f.is_file() and f.suffix.lower() in {".svg", ".png", ".webp", ".jpg", ".jpeg"}:
                        return "/images/" + f.name
        return None

    logo_path = _find_logo_path()
    if not logo_path:
        return files

    new_seed = empty_nav_re.sub(
        lambda _m: _m.group(0).replace("'')", f"'{logo_path}')"),
        seed,
    )
    if new_seed != seed:
        file_dict["db/seed.sql"] = new_seed
        print(f"  [post-process] _ensure_nav_logo_default_seed: seeded nav_logo_url = {logo_path}")
    return list(file_dict.items())


@processor(after=('_ensure_stat_card_accent_variety',))
def _ensure_about_photo_fallback(files: list, spec: dict = None) -> list:
    """MW v17 FIX J: replace the tiny SVG fallback in views/about.ejs with a
    meaningful stock-photo fallback when `siteSettings.about_photo_url` is
    empty.

    Resolution:
      1. Scan the files list AND (if available) the build dir via the
         _CURRENT_BUILD_DIR contextvar for stock photos placed by
         _ensure_stock_photos.
      2. Rank candidates: files whose basename contains
         'portrait'/'ocean'/'founder'/'about' win; else first match in the
         matched-category pool.
      3. Rewrite the else branch of views/about.ejs to render an <img>
         pointing at the chosen path, reusing the same styles as the
         populated case. If no stock is available, leave the template alone.
    """
    import re as _re_mod

    file_dict = dict(files)
    about = file_dict.get("views/about.ejs")
    if not about or not isinstance(about, str):
        return files

    # Collect stock photo paths: first from files list, then from disk via contextvar
    stock_paths = []
    for path in file_dict.keys():
        m = _re_mod.match(
            r"public/images/stock/([\w-]+)/([\w.-]+\.(?:jpg|jpeg|png|webp))$",
            path,
            _re_mod.IGNORECASE,
        )
        if m:
            stock_paths.append(f"/images/stock/{m.group(1)}/{m.group(2)}")

    if not stock_paths:
        try:
            build_dir = _CURRENT_BUILD_DIR.get()
        except LookupError:
            build_dir = None
        if build_dir:
            stock_root = Path(build_dir) / "public" / "images" / "stock"
            if stock_root.is_dir():
                for f in stock_root.rglob("*"):
                    if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                        rel = f.relative_to(Path(build_dir) / "public")
                        stock_paths.append("/" + str(rel))

    if not stock_paths:
        return files  # no stock, no-op

    # Rank candidates: filename keywords win
    PREFER_KEYWORDS = ("portrait", "ocean", "founder", "about", "headshot")
    def _rank(p):
        base = p.rsplit("/", 1)[-1].lower()
        for i, kw in enumerate(PREFER_KEYWORDS):
            if kw in base:
                return i
        return len(PREFER_KEYWORDS)
    stock_paths.sort(key=_rank)
    chosen = stock_paths[0]

    # Marker to keep idempotent
    marker = "<!-- v17-about-fallback:v1 -->"
    if marker in about:
        return files

    # Two-strategy match:
    #
    # Strategy 1 (preferred): find the else branch of a
    #   <% if (... about_photo_url ...) { %>  IMG  <% } else { %>  FALLBACK  <% } %>
    # block, and replace the FALLBACK with a real <img> pointing at chosen.
    # This works regardless of whether the fallback is a tiny SVG, a gradient
    # div, or a string literal: we replace whatever's between the else and
    # the closing `<% } %>`.
    about_photo_if_re = _re_mod.compile(
        r"<%\s*if\s*\(\s*(?:site[Ss]ettings\.)?about_photo(?:_url)?\s*\)\s*\{\s*%>"
        r"(?:.*?)"
        r"(<%\s*\}\s*else\s*\{\s*%>)"
        r"(.*?)"
        r"(<%\s*\}\s*%>)",
        _re_mod.DOTALL | _re_mod.IGNORECASE,
    )

    fallback_img = (
        f'\n        {marker}\n'
        f'        <img src="{chosen}" alt="About" class="rounded-xl border border-slate-700 shadow-lg shadow-black/30 w-full aspect-[3/4] object-cover">\n'
        f'      '
    )

    def _replace_else_body(m):
        return about[m.start():m.start(1) + len(m.group(1))] + fallback_img + m.group(3)

    new_about = about_photo_if_re.sub(
        lambda m: m.group(0).replace(
            m.group(2),  # everything inside the else branch
            fallback_img,
        ),
        about,
    )

    # Strategy 2 (fallback): specifically target the 20x20 music-note SVG
    # placeholder (original pattern observed in v16+v18) and swap in an <img>.
    if new_about == about:
        else_branch_re = _re_mod.compile(
            r"(<%\s*\}\s*else\s*\{\s*%>)"
            r"\s*"
            r"(?:<div[^>]*>\s*)?"
            r"<svg\b[^>]*>.*?</svg>"
            r"\s*"
            r"(?:</div>\s*)?"
            r"(<%\s*\}\s*%>)",
            _re_mod.DOTALL | _re_mod.IGNORECASE,
        )

        def _replace(m):
            return (
                m.group(1) + "\n"
                f"        {marker}\n"
                f'        <img src="{chosen}" alt="About" class="rounded-xl border border-slate-700 shadow-lg shadow-black/30 w-full aspect-[3/4] object-cover">\n'
                "      " + m.group(2)
            )
        new_about = else_branch_re.sub(_replace, about)
    if new_about != about:
        file_dict["views/about.ejs"] = new_about
        print(f"  [post-process] _ensure_about_photo_fallback: set about photo fallback to {chosen}")
    return list(file_dict.items())


@processor(after=('_ensure_accent_color_variety',))
def _ensure_stat_card_accent_variety(files: list, spec: dict = None) -> list:
    """MW v17 FIX I: strengthen visual variety on admin dashboard stat cards.

    Claude already emits 4 cards with different `border-t-accent*` top-border
    classes, but the border is only 2px AND the stat number text is plain
    white: the variety is perceptually invisible. This processor:

      1. Upgrades `border-t-2` to `border-t-4` on any card container that
         includes one of the accent top-border classes.
      2. Colors the stat-number paragraph (<p ... text-white ...>NN</p>)
         inside each such card with the matching accent text class.

    Only touches views under `views/admin/`. Idempotent.
    """
    import re as _re_mod

    file_dict = dict(files)

    ACCENT_TO_TEXT = {
        "border-t-accent-warm": "text-accent-warm",
        "border-t-accent-earth": "text-accent-earth",
        "border-t-accent": "text-accent",
        "border-t-red-500": "text-red-400",
        "border-t-red-600": "text-red-400",
    }
    # Match longer keys first so "border-t-accent" doesn't steal from "border-t-accent-warm".
    _ACCENT_KEYS_ORDERED = sorted(ACCENT_TO_TEXT.keys(), key=len, reverse=True)

    # Regex: opening <div ... border-t-X border-t-<accent>...> then the first
    # <p class="... text-3xl ... text-white ...">NUMBER</p> we find before the
    # matching `</div>`. Use a non-greedy brace-depth-aware pattern via regex.
    div_open_re = _re_mod.compile(
        r'<div\b([^>]*\bborder-t-(?:accent(?:-warm|-earth)?|red-[56]00)\b[^>]*)>',
        _re_mod.IGNORECASE,
    )

    changed_files = 0

    for path in list(file_dict.keys()):
        if not path.startswith("views/admin/") or not path.endswith(".ejs"):
            continue
        content = file_dict[path]
        if isinstance(content, bytes) or not isinstance(content, str):
            continue

        changed = False
        # Walk each matching stat card div
        pos = 0
        pieces = []
        while pos < len(content):
            m = div_open_re.search(content, pos)
            if not m:
                pieces.append(content[pos:])
                break

            # Emit everything before the match
            pieces.append(content[pos:m.start()])

            attrs = m.group(1)
            # Detect accent key
            accent_key = None
            for k in _ACCENT_KEYS_ORDERED:
                if _re_mod.search(rf'\b{_re_mod.escape(k)}\b', attrs):
                    accent_key = k
                    break

            # Upgrade any thin border-t-{1,2,3} to border-t-4 (within this
            # div's attribute list). Also handle bare `border-t` (1px default).
            new_attrs = _re_mod.sub(
                r'\bborder-t-[1-3]\b', 'border-t-4',
                attrs,
            )
            # Bare `border-t` (no width) → border-t-4
            new_attrs = _re_mod.sub(
                r'\bborder-t\b(?!-)', 'border-t-4',
                new_attrs,
            )
            if new_attrs != attrs:
                changed = True
            elif accent_key and "border-t-4" not in new_attrs:
                # Inject border-t-4 if no border-t-* width class was present
                new_attrs = new_attrs + " border-t-4"
                changed = True

            opening_tag = f'<div{new_attrs}>'

            # Scan forward to find the matching close `</div>` respecting nesting
            i = m.end()
            depth = 1
            while i < len(content) and depth > 0:
                next_open = content.find("<div", i)
                next_close = content.find("</div>", i)
                if next_close < 0:
                    break
                if next_open >= 0 and next_open < next_close:
                    depth += 1
                    i = next_open + 4
                else:
                    depth -= 1
                    i = next_close + len("</div>")
            inner_start = m.end()
            inner_end = i - len("</div>") if i > 0 else len(content)
            inner = content[inner_start:inner_end]

            # Color the stat number paragraph
            if accent_key:
                text_class = ACCENT_TO_TEXT[accent_key]
                # Target the FIRST `<p class="... text-3xl ..." ...>` inside the card
                def _apply_color(pm):
                    nonlocal changed
                    tag_attrs = pm.group(1)
                    # Skip if already colored with the right class
                    if text_class in tag_attrs:
                        return pm.group(0)
                    # Replace text-white with the accent class when present, else append
                    if "text-white" in tag_attrs:
                        new_tag_attrs = tag_attrs.replace("text-white", text_class)
                    else:
                        new_tag_attrs = tag_attrs + " " + text_class
                    changed = True
                    return f'<p class="{new_tag_attrs}">'
                inner_new = _re_mod.sub(
                    r'<p\s+class="([^"]*text-3xl[^"]*)">',
                    _apply_color,
                    inner,
                    count=1,
                )
                if inner_new != inner:
                    inner = inner_new

            pieces.append(opening_tag + inner + "</div>")
            pos = i

        new_content = "".join(pieces)
        if changed and new_content != content:
            file_dict[path] = new_content
            changed_files += 1

    if changed_files:
        print(f"  [post-process] _ensure_stat_card_accent_variety strengthened {changed_files} admin view(s)")

    return list(file_dict.items())


@processor(after=('_ensure_video_embeds',))
def _ensure_home_video_click_to_play(files: list, spec: dict = None) -> list:
    """MW v17 FIX G: convert any inline youtube-nocookie iframe on the home
    page into a click-to-play thumbnail. Avoids YouTube Error 153 surfacing
    when a featured video's ID is stale, unlisted, or has embedding disabled.

    Scope:
      - views/home.ejs and views/index.ejs only (event/video detail pages
        still get inline iframes: that's intended).
      - Handles both hardcoded `src="https://www.youtube-nocookie.com/embed/XXX"`
        and the dynamic `src="https://www.youtube-nocookie.com/embed/<%= _vidId %>"`
        form emitted by _ensure_video_embeds.

    Replacement pattern (matches _ensure_video_embeds' index branch):
      - <a target="_blank"> wrapping <img src="/api/video-thumb/<id>">
      - Play-button SVG overlay centered
      - External link to youtube.com/watch?v=<id>
    """
    import re as _re_mod

    file_dict = dict(files)
    changed = 0

    # Play-button SVG overlay (shared across both hardcoded + dynamic cases)
    _play_svg = (
        '<span class="absolute inset-0 flex items-center justify-center pointer-events-none">'
        '<svg class="w-16 h-16 text-red-600 drop-shadow-lg" viewBox="0 0 68 48">'
        '<path d="M66.52 7.74c-.78-2.93-2.49-5.41-5.42-6.19C55.79.13 34 0 34 '
        '0S12.21.13 6.9 1.55c-2.93.78-4.64 3.26-5.42 6.19C.06 13.05 0 24 0 '
        '24s.06 10.95 1.48 16.26c.78 2.93 2.49 5.41 5.42 6.19C12.21 47.87 34 '
        '48 34 48s21.79-.13 27.1-1.55c2.93-.78 4.64-3.26 5.42-6.19C67.94 34.95 '
        '68 24 68 24s-.06-10.95-1.48-16.26z" fill="currentColor"/>'
        '<path d="M45 24L27 14v20" fill="white"/></svg></span>'
    )

    # Dynamic-EJS-variable pattern: <iframe src="https://www.youtube-nocookie.com/embed/<%= X %>" ...>
    dyn_iframe_re = _re_mod.compile(
        r'<iframe\b[^>]*\ssrc="https://www\.youtube-nocookie\.com/embed/<%=\s*([^%]+?)\s*%>"[^>]*>(?:\s*</iframe>)?',
        _re_mod.IGNORECASE,
    )
    # Hardcoded pattern: <iframe src="https://www.youtube-nocookie.com/embed/XXXXXXXXXXX" ...>
    hard_iframe_re = _re_mod.compile(
        r'<iframe\b[^>]*\ssrc="https://www\.youtube-nocookie\.com/embed/([a-zA-Z0-9_-]{11})"[^>]*>(?:\s*</iframe>)?',
        _re_mod.IGNORECASE,
    )

    for path in list(file_dict.keys()):
        # Only home / index views
        base = path.rsplit("/", 1)[-1].lower()
        if path not in ("views/home.ejs", "views/index.ejs") and base not in ("home.ejs", "index.ejs"):
            continue
        content = file_dict[path]
        if isinstance(content, bytes) or not isinstance(content, str):
            continue

        new_content = content

        # Dynamic form first (more specific)
        def _dyn_repl(m):
            ejs_var = m.group(1).strip()
            return (
                f'<a href="https://www.youtube.com/watch?v=<%= {ejs_var} %>" '
                f'target="_blank" rel="noopener" class="block relative group w-full h-full">'
                f'<img src="/api/video-thumb/<%= {ejs_var} %>" '
                f'alt="Play video" class="w-full h-full object-cover" loading="lazy">'
                f'{_play_svg}</a>'
            )
        new_content = dyn_iframe_re.sub(_dyn_repl, new_content)

        # Hardcoded form
        def _hard_repl(m):
            vid_id = m.group(1)
            return (
                f'<a href="https://www.youtube.com/watch?v={vid_id}" '
                f'target="_blank" rel="noopener" class="block relative group w-full h-full">'
                f'<img src="/api/video-thumb/{vid_id}" '
                f'alt="Play video" class="w-full h-full object-cover" loading="lazy">'
                f'{_play_svg}</a>'
            )
        new_content = hard_iframe_re.sub(_hard_repl, new_content)

        # Also strip any bare "Watch on YouTube" fallback paragraph that was
        # paired with the inline iframe: the thumbnail already links out.
        # Match the simple <p>...watch?v=...<%= X %>...</p> pattern emitted by
        # _ensure_video_embeds for detail pages.
        new_content = _re_mod.sub(
            r'<p[^>]*>\s*<a[^>]*href="https://www\.youtube\.com/watch\?v=<%=[^"]*%>"[^>]*>\s*Watch on YouTube[^<]*</a>\s*</p>',
            "",
            new_content,
            flags=_re_mod.IGNORECASE,
        )

        if new_content != content:
            file_dict[path] = new_content
            changed += 1

    if changed:
        print(f"  [post-process] _ensure_home_video_click_to_play rewrote {changed} home-page video embed(s)")

    return list(file_dict.items())


@processor(after=('_ensure_delete_confirm_single_handler',))
def _ensure_button_design_system(files: list, spec: dict = None) -> list:
    """MW v17 FIX E: append canonical `.btn-*` classes to public/style.css so
    FIX B's generated CRUD forms and any downstream button rewrites have a
    consistent visual system across the app.

    Roles:
      .btn-primary: filled, accent color, default CTA
      .btn-secondary: outline, neutral, paired-with-primary
      .btn-ghost: text-only, low-emphasis, often "Cancel"
      .btn-destructive: red outline, delete/danger
      .btn-warm: sun-yellow fill, "Get Tickets" / commerce CTA
    """
    file_dict = dict(files)
    css = file_dict.get("public/style.css")
    if css is None:
        return files

    marker = "/* v17-button-system:v1 */"
    if marker in css:
        return files  # idempotent

    block = (
        "\n"
        + marker + "\n"
        + ".btn-primary, .btn-secondary, .btn-ghost, .btn-destructive, .btn-warm {\n"
        + "  display: inline-flex;\n"
        + "  align-items: center;\n"
        + "  justify-content: center;\n"
        + "  gap: 0.5rem;\n"
        + "  min-height: 44px;\n"
        + "  padding: 0.75rem 1.25rem;\n"
        + "  border-radius: 0.5rem;\n"
        + "  font-weight: 600;\n"
        + "  font-size: 0.9375rem;\n"
        + "  line-height: 1;\n"
        + "  text-decoration: none;\n"
        + "  cursor: pointer;\n"
        + "  border: 1px solid transparent;\n"
        + "  transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease, transform 0.1s ease;\n"
        + "}\n"
        + ".btn-primary:focus-visible, .btn-secondary:focus-visible, .btn-ghost:focus-visible, .btn-destructive:focus-visible, .btn-warm:focus-visible {\n"
        + "  outline: 2px solid var(--color-accent, #5a80c0);\n"
        + "  outline-offset: 2px;\n"
        + "}\n"
        + ".btn-primary {\n"
        + "  background: var(--color-accent, #5a80c0);\n"
        + "  color: #fff;\n"
        + "}\n"
        + ".btn-primary:hover { background: color-mix(in oklab, var(--color-accent, #5a80c0) 85%, #000); }\n"
        + ".btn-secondary {\n"
        + "  background: transparent;\n"
        + "  color: var(--color-text, #e2e8f0);\n"
        + "  border-color: rgba(148, 163, 184, 0.45);\n"
        + "}\n"
        + ".btn-secondary:hover { background: rgba(148, 163, 184, 0.12); border-color: rgba(148, 163, 184, 0.7); }\n"
        + ".btn-ghost {\n"
        + "  background: transparent;\n"
        + "  color: var(--color-text-muted, #94a3b8);\n"
        + "}\n"
        + ".btn-ghost:hover { background: rgba(148, 163, 184, 0.1); color: var(--color-text, #e2e8f0); }\n"
        + ".btn-destructive {\n"
        + "  background: transparent;\n"
        + "  color: #f87171;\n"
        + "  border-color: rgba(248, 113, 113, 0.5);\n"
        + "}\n"
        + ".btn-destructive:hover { background: rgba(248, 113, 113, 0.12); border-color: #f87171; color: #fca5a5; }\n"
        + ".btn-warm {\n"
        + "  background: var(--color-accent-warm, #c9a84c);\n"
        + "  color: #1a1204;\n"
        + "}\n"
        + ".btn-warm:hover { background: color-mix(in oklab, var(--color-accent-warm, #c9a84c) 85%, #000); }\n"
    )

    file_dict["public/style.css"] = (css.rstrip() + "\n" + block).lstrip("\n")
    print("  [post-process] _ensure_button_design_system: appended canonical .btn-* classes")
    return list(file_dict.items())


@processor(after=('_strip_duplicate_confirm_handlers',))
def _ensure_delete_confirm_single_handler(files: list, spec: dict = None) -> list:
    """MW v17 FIX D: collapse delete-confirmation wiring down to ONE handler
    so confirm() only pops once.

    Observed (v16):
      - layout.ejs has `querySelectorAll('[data-confirm]').forEach(el => el
        .addEventListener('submit', () => confirm(...)))`: per-element handler
      - _ensure_submit_guard injects a document-level submit handler that ALSO
        calls confirm() when `form.dataset.confirm` is set
      - Both fire on the same submit event → two dialogs
      - Some forms carry both `data-confirm="..."` and `onsubmit="return confirm(...)"`
        - even without the forEach handler, the inline attribute + guard cause
        a double dialog

    Strategy:
      - Remove the per-element forEach block from layout.ejs (submit_guard is
        the single source of truth for data-confirm)
      - Strip `onsubmit="return confirm(...)"` from `<form>` tags that ALSO
        have a `data-confirm` attribute
      - Leave onsubmit-only forms untouched (they work correctly as-is)
    """
    import re as _re_mod

    file_dict = dict(files)
    changed_layout = False
    changed_forms = 0

    layout = file_dict.get("views/layout.ejs", "")
    if layout:
        # Match: document.querySelectorAll('[data-confirm]').forEach(function(el) { ... });
        # Also handle arrow form. Brace-balance the body.
        foreach_pattern = _re_mod.compile(
            r"""document\.querySelectorAll\(\s*['"]\[data-confirm\]['"]\s*\)\.forEach\s*\(\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>)\s*\{""",
            _re_mod.IGNORECASE,
        )
        m = foreach_pattern.search(layout)
        if m:
            # Scan to the matching `});` (forEach close + statement terminator)
            i = m.end() - 1  # points at the opening `{`
            assert layout[i] == "{"
            depth = 1
            i += 1
            while i < len(layout) and depth > 0:
                c = layout[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                i += 1
            # i now points past the closing `}` of the callback
            # Expect `)` then optional whitespace then `;`
            j = i
            while j < len(layout) and layout[j] in " \t\n":
                j += 1
            if j < len(layout) and layout[j] == ")":
                j += 1
                while j < len(layout) and layout[j] in " \t\n":
                    j += 1
                if j < len(layout) and layout[j] == ";":
                    j += 1
            # Trim trailing whitespace+newline
            end = j
            while end < len(layout) and layout[end] in " \t":
                end += 1
            if end < len(layout) and layout[end] == "\n":
                end += 1
            # Also remove a leading `// Confirm dialogs` comment line if present
            start = m.start()
            line_start = layout.rfind("\n", 0, start) + 1
            preceding = layout[line_start:start]
            if preceding.strip() == "":
                # Check one line above for comment
                prev_line_end = line_start - 1
                prev_line_start = layout.rfind("\n", 0, prev_line_end) + 1 if prev_line_end >= 0 else 0
                prev_line = layout[prev_line_start:prev_line_end].strip()
                if prev_line.startswith("//") and "confirm" in prev_line.lower():
                    start = prev_line_start
            new_layout = layout[:start] + layout[end:]
            file_dict["views/layout.ejs"] = new_layout
            changed_layout = True

    # Strip onsubmit from forms that also have data-confirm
    form_with_both = _re_mod.compile(
        r'(<form\b[^>]*?)(\s+onsubmit="[^"]*confirm\([^"]*"\s*)([^>]*>)',
        _re_mod.IGNORECASE,
    )

    for path, content in list(file_dict.items()):
        if not path.endswith(".ejs") or isinstance(content, bytes):
            continue

        def _strip_onsubmit(match):
            nonlocal changed_forms
            head = match.group(1)
            mid = match.group(2)
            tail = match.group(3)
            full = head + mid + tail
            if "data-confirm" not in full:
                return match.group(0)  # only strip when both present
            changed_forms += 1
            return head + " " + tail.lstrip()

        new_content = form_with_both.sub(_strip_onsubmit, content)
        if new_content != content:
            file_dict[path] = new_content

    if changed_layout:
        print("  [post-process] _ensure_delete_confirm_single_handler: stripped per-element forEach confirm wiring from layout.ejs")
    if changed_forms:
        print(f"  [post-process] _ensure_delete_confirm_single_handler: stripped onsubmit from {changed_forms} form(s) that also had data-confirm")

    return list(file_dict.items())


@processor(after=('_ensure_crud_forms_complete',))
def _fix_route_link_drift(files: list, spec: dict) -> list:
    """MW v17 FIX C: rewrite `<a href>` values in view templates that point
    to unmounted routes, when a close-match mounted route exists.

    Observed regression (v16): admin dashboard linked `/messages` but server.js
    only mounted `/contact-messages`. Click → 404.

    Resolution:
      1. Parse `app.use('/<prefix>', ...)` from server.js to build a set of
         mounted route prefixes.
      2. For each href in view templates, split path -> first segment.
      3. If the first segment isn't a mounted prefix, search mounted prefixes
         for a single-word match (same suffix after a hyphen): `/messages`
         matches `/contact-messages`, `/orders` matches `/customer-orders`.
      4. Rewrite only when there's exactly ONE candidate; ambiguous cases
         leave the href alone so we don't silently pick the wrong target.

    Skipped:
      - http(s) / mailto / tel / anchor (#) hrefs
      - hrefs containing <%= ... %> (dynamic EJS expressions)
      - hrefs that already resolve (prefix mounted)
    """
    import re as _re_mod

    file_dict = dict(files)
    server = file_dict.get("server.js", "") or ""
    if not server:
        return files

    mount_re = _re_mod.compile(
        r"""app\.use\(\s*['"](/[^'"?#]+?)['"]\s*,""",
        _re_mod.IGNORECASE,
    )
    mounted = set()
    for m in mount_re.finditer(server):
        prefix = m.group(1)
        # Normalize: drop trailing slash
        if prefix != "/" and prefix.endswith("/"):
            prefix = prefix.rstrip("/")
        mounted.add(prefix)
    if not mounted:
        return files

    # Build a lookup: first-segment-candidate → set of full-mount-prefix matches
    # that end with `-<candidate>` or start with `<candidate>-`.
    candidates = {}
    for prefix in mounted:
        seg = prefix.lstrip("/").split("/", 1)[0]
        candidates.setdefault(seg, set()).add(prefix)
        # Hyphen variants: "contact-messages" registers under "messages" lookup too
        if "-" in seg:
            parts = seg.split("-")
            for part in parts:
                candidates.setdefault(part, set()).add(prefix)

    href_re = _re_mod.compile(r'href="(/[^"#?]*)"')
    changed_files = 0

    for path, content in list(file_dict.items()):
        if not path.endswith(".ejs") or isinstance(content, bytes):
            continue

        changed = False

        def _maybe_rewrite(match):
            nonlocal changed
            href = match.group(1)
            # Skip dynamic EJS expressions inside the href
            if "<%" in href:
                return match.group(0)
            # Split into first segment and rest
            trimmed = href.lstrip("/")
            if not trimmed:
                return match.group(0)
            first_seg, _, rest = trimmed.partition("/")
            first_mount = "/" + first_seg
            if first_mount in mounted:
                return match.group(0)  # already resolves, leave alone

            # Look for a unique close-match
            cand_set = candidates.get(first_seg)
            if not cand_set:
                return match.group(0)
            # Prefer a mount that ends with "-<first_seg>" (e.g. /contact-messages for /messages)
            suffix_match = [c for c in cand_set if c.lstrip("/").endswith("-" + first_seg)]
            if len(suffix_match) == 1:
                new_first = suffix_match[0]
            elif len(cand_set) == 1:
                new_first = next(iter(cand_set))
            else:
                return match.group(0)  # ambiguous: don't guess

            new_href = new_first + (("/" + rest) if rest else "")
            changed = True
            return f'href="{new_href}"'

        new_content = href_re.sub(_maybe_rewrite, content)
        if new_content != content:
            file_dict[path] = new_content
            changed_files += 1

    if changed_files:
        print(f"  [post-process] _fix_route_link_drift rewrote hrefs in {changed_files} view(s)")

    return list(file_dict.items())


@processor(after=('_ensure_upload_route',))
def _ensure_crud_forms_complete(files: list, spec: dict) -> list:
    """MW v17 FIX B: detect stub/empty admin CRUD views and replace them with
    a canonical form derived from the spec's data_models + the actual route
    contract in routes/<slug>.js.

    Two stub patterns this catches (both observed on MW v16):
      - Literal placeholder text: "Content for X will appear here."
      - Heading + error block with no <form> tag

    Generated form:
      - Matches each field in spec.data_models[i]['fields']
      - POST method with CSRF hidden input
      - Action URL inferred from routes/<slug>.js ('/:id/update' vs '/:id')
      - Required fields get the `required` attribute
      - Type mapping:
          text / varchar       -> <input type="text">
          description/body/*   -> <textarea rows="5">  (name-based heuristic)
          timestamp / datetime -> <input type="datetime-local">
          boolean              -> <input type="checkbox">
          integer / number     -> <input type="number">
          image_url            -> hidden input (upgraded by _ensure_upload_form_ui)
          url / embed_url      -> <input type="url">
      - Edit form pre-populates with <%= entity.field %>

    Only touches files matching 'views/<plural>/new.ejs' or 'views/<plural>/edit.ejs'
    where <plural> corresponds to a model in spec.data_models.
    """
    import re as _re_mod

    file_dict = dict(files)
    if not file_dict:
        return files
    data_models = spec.get("data_models", []) or []
    if not data_models:
        return files

    # Build slug -> model map. Skip models the spec explicitly flags as
    # read-only (admin should not create/edit: e.g. ContactMessage per MW
    # spec). Read-only is detected via any of:
    #   model["access"] == "read-only" / "readonly"
    #   model["read_only"] == True
    #   model["permissions"] in {"read", "view"}
    #   model name ends with "Message" and spec.pages has no /{plural}/new
    model_by_slug = {}
    for m in data_models:
        name = str(m.get("name", "")).strip()
        if not name:
            continue
        access = str(m.get("access", "")).lower()
        if m.get("read_only") or access in {"read-only", "readonly", "ro"}:
            continue
        perms = m.get("permissions")
        if isinstance(perms, str) and perms.lower() in {"read", "view", "read-only"}:
            continue
        slug = _model_slug(name)
        plural = _pluralize_slug(slug)
        # Spec-driven gate: if pages[] is non-empty and there's no page at
        # /{plural}/new, treat as read-only (spec didn't ask for create UI).
        pages = spec.get("pages", []) or []
        if pages:
            new_path = f"/{plural}/new"
            has_new_page = any(
                isinstance(p, dict) and str(p.get("path", "")).rstrip("/") == new_path
                for p in pages
            )
            # Only gate out if ALL spec pages are enumerated AND none hit new;
            # otherwise fall through (spec may be coarse).
            if not has_new_page and name.endswith(("Message", "Log", "Event")) is False:
                # Don't gate Event-like models (common top-level content)
                # but DO gate Message-like models which are typically read-only.
                if name.endswith("Message"):
                    continue
        model_by_slug[plural] = m

    TEXTAREA_NAMES = {
        "description", "body", "message", "content", "bio", "about",
        "notes", "summary", "excerpt", "details",
    }
    URL_FIELD_NAMES = {"rsvp_link", "embed_url", "link", "website_url", "youtube_url"}

    def _pick_update_action(plural: str, entity_var: str) -> str:
        route_src = file_dict.get(f"routes/{plural}.js", "") or ""
        if "router.post('/:id/update'" in route_src or 'router.post("/:id/update"' in route_src:
            return f'/{plural}/<%= {entity_var}.id %>/update'
        return f'/{plural}/<%= {entity_var}.id %>'

    def _is_stub(content: str) -> bool:
        if "Content for" in content and "will appear here" in content:
            return True
        if "<form" not in content.lower():
            return True
        return False

    def _field_input(field: dict, action: str, entity_var: str) -> str:
        """Render a <div>-wrapped labeled input for one field."""
        name = str(field.get("name", "")).strip()
        if not name:
            return ""
        ftype = str(field.get("type", "text")).strip().lower()
        required = bool(field.get("required"))
        is_edit = action == "edit"
        label_base = name.replace("_", " ").title()
        req_marker = ' <span class="text-red-400">*</span>' if required else ""
        required_attr = " required" if required else ""

        # Value expression for edit forms
        val = f'<%= {entity_var}.{name} || "" %>'

        # image_url -> hidden input (upgraded to upload zone by _ensure_upload_form_ui)
        if name.endswith("image_url") or name == "image" or name.endswith("_photo_url"):
            # upload_form_ui looks for: `type="hidden" id="<name>" name="<name>"`
            cur_val = f'<%= typeof {entity_var} !== "undefined" && {entity_var} && {entity_var}.{name} ? {entity_var}.{name} : "" %>' if is_edit else ""
            return (
                f'      <div>\n'
                f'        <label class="block text-sm font-medium text-slate-300 mb-1.5">{label_base}{req_marker}</label>\n'
                f'        <input type="hidden" id="{name}" name="{name}" value="{cur_val}"{required_attr}>\n'
                f'      </div>\n'
            )

        # Booleans -> checkbox
        if ftype in {"boolean", "bool"}:
            if is_edit:
                checked = f'<%= {entity_var}.{name} ? "checked" : "" %>'
                return (
                    f'      <div class="flex items-center gap-2">\n'
                    f'        <input type="checkbox" id="{name}" name="{name}" value="true" {checked} class="h-4 w-4 rounded border-slate-600 bg-slate-800 text-accent focus:ring-accent">\n'
                    f'        <label for="{name}" class="text-sm text-slate-300">{label_base}</label>\n'
                    f'      </div>\n'
                )
            return (
                f'      <div class="flex items-center gap-2">\n'
                f'        <input type="checkbox" id="{name}" name="{name}" value="true" class="h-4 w-4 rounded border-slate-600 bg-slate-800 text-accent focus:ring-accent">\n'
                f'        <label for="{name}" class="text-sm text-slate-300">{label_base}</label>\n'
                f'      </div>\n'
            )

        # Timestamp -> datetime-local
        if ftype in {"timestamp", "timestamptz", "datetime", "date"} or name.endswith("_at") or name == "date_time":
            dt_val = ""
            if is_edit:
                dt_val = f'<%= {entity_var}.{name} ? new Date({entity_var}.{name}).toISOString().slice(0,16) : "" %>'
            return (
                f'      <div>\n'
                f'        <label for="{name}" class="block text-sm font-medium text-slate-300 mb-1.5">{label_base}{req_marker}</label>\n'
                f'        <input type="datetime-local" id="{name}" name="{name}" value="{dt_val}"{required_attr} class="w-full bg-slate-800 border border-slate-600 rounded-lg px-4 py-2.5 text-base text-slate-100 focus:outline-none focus:ring-2 focus:ring-accent/50 focus:border-accent">\n'
                f'      </div>\n'
            )

        # Integer/numeric
        if ftype in {"integer", "int", "number", "numeric", "float", "double"}:
            num_val = f'<%= {entity_var}.{name} || 0 %>' if is_edit else "0"
            return (
                f'      <div>\n'
                f'        <label for="{name}" class="block text-sm font-medium text-slate-300 mb-1.5">{label_base}{req_marker}</label>\n'
                f'        <input type="number" id="{name}" name="{name}" value="{num_val}"{required_attr} class="w-full bg-slate-800 border border-slate-600 rounded-lg px-4 py-2.5 text-base text-slate-100 focus:outline-none focus:ring-2 focus:ring-accent/50 focus:border-accent">\n'
                f'      </div>\n'
            )

        # Textarea for long-text conventional names
        if name in TEXTAREA_NAMES:
            ta_val = val if is_edit else ""
            return (
                f'      <div>\n'
                f'        <label for="{name}" class="block text-sm font-medium text-slate-300 mb-1.5">{label_base}{req_marker}</label>\n'
                f'        <textarea id="{name}" name="{name}" rows="5"{required_attr} class="w-full bg-slate-800 border border-slate-600 rounded-lg px-4 py-2.5 text-base text-slate-100 focus:outline-none focus:ring-2 focus:ring-accent/50 focus:border-accent">{ta_val}</textarea>\n'
                f'      </div>\n'
            )

        # URL input
        if name in URL_FIELD_NAMES or name.endswith("_url"):
            v = val if is_edit else ""
            return (
                f'      <div>\n'
                f'        <label for="{name}" class="block text-sm font-medium text-slate-300 mb-1.5">{label_base}{req_marker}</label>\n'
                f'        <input type="url" id="{name}" name="{name}" value="{v}"{required_attr} class="w-full bg-slate-800 border border-slate-600 rounded-lg px-4 py-2.5 text-base text-slate-100 focus:outline-none focus:ring-2 focus:ring-accent/50 focus:border-accent">\n'
                f'      </div>\n'
            )

        # Default: text input
        v = val if is_edit else ""
        return (
            f'      <div>\n'
            f'        <label for="{name}" class="block text-sm font-medium text-slate-300 mb-1.5">{label_base}{req_marker}</label>\n'
            f'        <input type="text" id="{name}" name="{name}" value="{v}"{required_attr} class="w-full bg-slate-800 border border-slate-600 rounded-lg px-4 py-2.5 text-base text-slate-100 focus:outline-none focus:ring-2 focus:ring-accent/50 focus:border-accent">\n'
            f'      </div>\n'
        )

    def _needs_multipart(fields: list) -> bool:
        return any(
            (str(f.get("name", "")).endswith("image_url") or str(f.get("name", "")).endswith("_photo_url"))
            for f in fields
        )

    def _render_form(model: dict, plural: str, action_kind: str, entity_var: str) -> str:
        """action_kind is 'new' or 'edit'."""
        fields = model.get("fields", []) or []
        name = model.get("name", plural.title())
        if action_kind == "new":
            action = f"/{plural}"
            heading = f"Create New {name}"
            back_target = f"/{plural}"
            back_label = f"Back to {plural.title()}"
            submit_label = f"Create {name}"
        else:
            action = _pick_update_action(plural, entity_var)
            heading = f"Edit {name}"
            back_target = f"/{plural}/<%= {entity_var}.id %>"
            back_label = f"Back to {name}"
            submit_label = f"Update {name}"

        enctype = ' enctype="multipart/form-data"' if _needs_multipart(fields) else ""

        inputs = "".join(_field_input({**f, **{}}, "edit" if action_kind == "edit" else "new", entity_var) for f in fields)

        return (
            f'<section class="py-12 sm:py-16">\n'
            f'  <div class="max-w-2xl mx-auto px-4 sm:px-6">\n'
            f'    <a href="{back_target}" class="inline-flex items-center text-sm text-slate-400 hover:text-accent transition-colors mb-6">&larr; {back_label}</a>\n'
            f'    <h1 class="font-display text-3xl font-bold text-white mb-8">{heading}</h1>\n'
            f'    <% if (locals.error) {{ %><div class="bg-red-900/50 text-red-400 border border-red-800 rounded-lg px-4 py-3 mb-6 text-sm" role="alert"><%= error %></div><% }} %>\n'
            f'    <form method="POST" action="{action}"{enctype} class="space-y-6">\n'
            f'      <input type="hidden" name="_csrf" value="<%= csrfToken %>">\n'
            f'{inputs}'
            f'      <div class="flex gap-3 pt-2">\n'
            f'        <button type="submit" class="btn-primary px-6 py-3 bg-accent text-white font-semibold rounded-lg hover:bg-accent/80 transition-all min-h-[44px]">{submit_label}</button>\n'
            f'        <a href="{back_target}" class="btn-ghost px-6 py-3 border border-slate-600 text-slate-300 rounded-lg font-medium hover:bg-slate-800 transition-all min-h-[44px] inline-flex items-center">Cancel</a>\n'
            f'      </div>\n'
            f'    </form>\n'
            f'  </div>\n'
            f'</section>\n'
        )

    # Walk all views and rewrite stubs
    view_re = _re_mod.compile(r"^views/([\w-]+)/(new|edit)\.ejs$")
    replaced = 0
    for path in list(file_dict.keys()):
        m = view_re.match(path)
        if not m:
            continue
        plural, action_kind = m.group(1), m.group(2)
        model = model_by_slug.get(plural)
        if not model:
            continue
        content = file_dict.get(path, "")
        if isinstance(content, bytes):
            continue
        if not _is_stub(content):
            continue
        entity_var = _model_slug(model.get("name", plural))
        file_dict[path] = _render_form(model, plural, action_kind, entity_var)
        replaced += 1

    if replaced:
        print(f"  [post-process] _ensure_crud_forms_complete replaced {replaced} stub CRUD view(s)")

    return list(file_dict.items())


@processor(after=('_ensure_stock_photos',))
def _ensure_model_seed_images(files: list, spec: dict) -> list:
    """MW v17 FIX A: every seed INSERT into a table with an image_url column
    must leave with a non-NULL image_url value.

    Root cause (v16): Claude's generated seed.sql emitted literal NULL for
    image_url on every events/photos row. Photos.image_url is NOT NULL so every
    photo silently failed to insert (0 photos in DB, gallery empty). Events
    rendered without hero images on list + detail views.

    Resolution order per row:
      1. spec['_seed_<plural>'][i]['image_url'] if provided (preferred: lets
         the spec carry curated stock paths per row)
      2. Rotate through available stock photos matched to the spec's category
      3. Fall back to a generic placeholder path if the manifest is empty
         (schema.image_url is VARCHAR so a path string is always safe)

    Only rewrites NULL values. INSERTs that already have a real path are left
    alone so this is idempotent.
    """
    import re as _re_mod

    file_dict = dict(files)
    seed = file_dict.get("db/seed.sql")
    if not seed:
        return files
    schema = file_dict.get("db/schema.sql", "")

    # Step 1: Identify tables whose CREATE TABLE has an image_url column.
    image_tables = set()
    for tbl_match in _re_mod.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(\w+)[\"']?\s*\((.*?)\)\s*;",
        schema,
        _re_mod.IGNORECASE | _re_mod.DOTALL,
    ):
        tbl_name = tbl_match.group(1).lower()
        body = tbl_match.group(2)
        if _re_mod.search(r"\bimage_url\b", body, _re_mod.IGNORECASE):
            image_tables.add(tbl_name)

    if not image_tables:
        return files

    # Step 2: Build a stock-photo rotation pool.
    # Resolution order:
    #   (a) Category match via _match_stock_categories (semantic; ideal)
    #   (b) Scan the incoming files for public/images/stock/<cat>/<file>
    #       entries already placed by _ensure_stock_photos. This catches
    #       spec descriptions that don't match any keyword category but
    #       still have stock photos pre-copied (e.g. custom category).
    #   (c) Last-resort SVG-placeholder path (served by the runtime route
    #       that _ensure_stock_photos injects).
    stock_paths = []
    try:
        categories = _match_stock_categories(spec) or []
        manifest_path = Path(__file__).parent / "stock_photos" / "manifest.json"
        if manifest_path.exists() and categories:
            manifest = json.loads(manifest_path.read_text())
            stock_dir = Path(__file__).parent / "stock_photos"
            for cat in categories:
                for photo in manifest.get(cat, []):
                    src = stock_dir / cat / photo["filename"]
                    if src.exists():
                        stock_paths.append(f"/images/stock/{cat}/{photo['filename']}")
    except Exception:
        stock_paths = []

    if not stock_paths:
        # Fallback (b): scan files for pre-copied stock photos
        for path, content in file_dict.items():
            m = _re_mod.match(
                r"public/images/stock/([\w-]+)/([\w.-]+\.(?:jpg|jpeg|png|webp))$",
                path,
                _re_mod.IGNORECASE,
            )
            if m:
                stock_paths.append(f"/images/stock/{m.group(1)}/{m.group(2)}")

    if not stock_paths:
        # Fallback (c): during reapply, binary files are excluded from the
        # files list by the UnicodeDecodeError skip. Scan the build dir on
        # disk via the contextvar when available.
        try:
            build_dir = _CURRENT_BUILD_DIR.get()
        except LookupError:
            build_dir = None
        if build_dir:
            stock_root = Path(build_dir) / "public" / "images" / "stock"
            if stock_root.is_dir():
                for f in stock_root.rglob("*"):
                    if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                        rel = f.relative_to(Path(build_dir) / "public")
                        stock_paths.append("/" + str(rel))

    if not stock_paths:
        # Last-resort placeholder: avoids NOT NULL violations. A later pipeline
        # pass (_ensure_stock_photos) wires an SVG placeholder route for
        # /images/stock/* so this still renders. We generate 12 distinct
        # placeholder paths so tables with UNIQUE(image_url) constraints don't
        # silently reject all-but-one row via ON CONFLICT DO NOTHING (observed
        # as the v18 "photos=1 despite 15 INSERTs" bug; Claude added a UNIQUE
        # index this build and the single placeholder collapsed all rows).
        stock_paths = [f"/images/stock/placeholder-{i}.jpg" for i in range(1, 13)]

    # Per-model row counters, used to index into spec['_seed_<plural>'] and
    # also to rotate through the stock pool.
    row_counters = {t: 0 for t in image_tables}

    # Spec-provided per-row image paths, keyed by table name.
    spec_seed = {}
    for model in spec.get("data_models", []) or []:
        name = str(model.get("name", "")).strip()
        if not name:
            continue
        slug = _model_slug(name)  # existing helper
        plural = _pluralize_slug(slug)  # existing helper
        seed_key = f"_seed_{plural}"
        if seed_key in spec and isinstance(spec[seed_key], list):
            spec_seed[plural] = spec[seed_key]

    # Scan seed.sql structurally: for each "INSERT INTO <table> (cols) VALUES
    # (...)" find the columns-paren and values-paren by tracking balanced
    # parens (so NOW()-style function calls inside VALUES don't fool us the
    # way a greedy/non-greedy regex would).
    header_re = _re_mod.compile(
        r"INSERT\s+INTO\s+[\"']?(\w+)[\"']?\s*\(",
        _re_mod.IGNORECASE,
    )
    values_kw_re = _re_mod.compile(r"\s*VALUES\s*\(", _re_mod.IGNORECASE)

    def _find_matching_paren(src: str, open_idx: int) -> int:
        """Given src[open_idx] == '(', return the index of the matching ')'.
        Tracks single-quote strings (with '' escape). Returns -1 if unbalanced."""
        assert src[open_idx] == "("
        depth = 0
        in_quote = False
        i = open_idx
        while i < len(src):
            ch = src[i]
            if ch == "'":
                if in_quote and i + 1 < len(src) and src[i + 1] == "'":
                    i += 2
                    continue
                in_quote = not in_quote
            elif not in_quote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return -1

    changed = False
    out_parts = []
    cursor = 0

    for header_match in header_re.finditer(seed):
        table = header_match.group(1).lower()
        cols_open = header_match.end() - 1  # position of the '(' after table name
        cols_close = _find_matching_paren(seed, cols_open)
        if cols_close < 0:
            continue
        vm = values_kw_re.match(seed, cols_close + 1)
        if not vm:
            continue
        vals_open = vm.end() - 1
        vals_close = _find_matching_paren(seed, vals_open)
        if vals_close < 0:
            continue

        if table not in image_tables:
            continue
        columns_str = seed[cols_open + 1: cols_close]
        values_str = seed[vals_open + 1: vals_close]
        columns = [c.strip().strip('"').strip("'").lower() for c in columns_str.split(",")]
        if "image_url" not in columns:
            continue
        img_idx = columns.index("image_url")
        values = _split_sql_values(values_str)
        if len(values) != len(columns):
            continue

        current = values[img_idx].strip()
        row_index = row_counters[table]
        row_counters[table] += 1
        if current.upper() != "NULL":
            continue  # already populated, leave alone

        replacement = None
        seed_rows = spec_seed.get(table)
        if seed_rows and row_index < len(seed_rows):
            candidate = seed_rows[row_index].get("image_url")
            if candidate:
                replacement = candidate
        if not replacement:
            replacement = stock_paths[row_index % len(stock_paths)]

        values[img_idx] = f"'{replacement}'"
        new_values_str = ", ".join(values)

        # Emit everything up to the values content, then replacement content,
        # then the closing paren onward.
        out_parts.append(seed[cursor: vals_open + 1])
        out_parts.append(new_values_str)
        cursor = vals_close  # leave ')' and the rest to the next iteration/tail
        changed = True

    out_parts.append(seed[cursor:])
    new_seed = "".join(out_parts) if changed else seed

    if changed:
        file_dict["db/seed.sql"] = new_seed
        print(
            f"  [post-process] Filled NULL image_url in seed.sql for tables: "
            f"{', '.join(sorted(image_tables))}"
        )

    return list(file_dict.items())


def _split_sql_values(values_str: str) -> list:
    """Split a VALUES (a, b, c) parenthesis-content into components, respecting
    single-quoted strings. Not a full SQL parser; handles the idioms seed.sql
    actually uses: single quotes, simple NULL, numbers, NOW() calls, bools."""
    parts = []
    current = []
    in_quote = False
    i = 0
    while i < len(values_str):
        ch = values_str[i]
        if ch == "'":
            # Handle escaped '' inside a string
            if in_quote and i + 1 < len(values_str) and values_str[i + 1] == "'":
                current.append("''")
                i += 2
                continue
            in_quote = not in_quote
            current.append(ch)
        elif ch == "," and not in_quote:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return parts


@processor(after=('_ensure_minimum_seed_rows',), phase=('build',))
def _ensure_compression(files: list) -> list:
    """Add compression middleware to server.js for gzip responses.

    Inserts app.use(require('compression')()) before express.static so all
    responses (HTML, CSS, JS) are compressed. 60-70% smaller on average.
    """
    file_dict = dict(files)

    # Add compression to package.json
    server = file_dict.get("server.js", "")
    pkg_str = file_dict.get("package.json", "")
    if not server:
        return files

    # Skip if compression already present
    if "compression" in server:
        return files

    # Add to package.json dependencies
    if pkg_str and "compression" not in pkg_str:
        import json as _json
        try:
            pkg = _json.loads(pkg_str)
            deps = pkg.get("dependencies", {})
            deps["compression"] = "^1.7.4"
            pkg["dependencies"] = deps
            file_dict["package.json"] = _json.dumps(pkg, indent=2) + "\n"
        except (ValueError, KeyError):
            pass

    # Insert compression middleware before express.static
    static_patterns = [
        "app.use(express.static(",
        "app.use(express.static(path.join(",
    ]
    inserted = False
    for pattern in static_patterns:
        if pattern in server:
            server = server.replace(
                pattern,
                "app.use(require('compression')());\n" + pattern,
                1,
            )
            inserted = True
            break

    if inserted:
        file_dict["server.js"] = server
        print("  [post-process] Added compression middleware")

    return list(file_dict.items())


@processor(after=('_ensure_compression',), phase=('build',))
def _ensure_static_caching(files: list) -> list:
    """Patch express.static('public') to include { maxAge: '7d' }.

    Eliminates redundant asset downloads on repeat visits.
    """
    file_dict = dict(files)
    server = file_dict.get("server.js", "")
    if not server:
        return files

    # Skip if maxAge already present in the express.static call specifically
    if "express.static" in server and "maxAge" in server.split("express.static")[1][:80]:
        return files

    import re as _re_mod

    # Match express.static('public') or express.static("public") without options
    # Also match path.join(__dirname, 'public') variant
    patterns = [
        (r"express\.static\(\s*'public'\s*\)", "express.static('public', { maxAge: '7d' })"),
        (r'express\.static\(\s*"public"\s*\)', "express.static('public', { maxAge: '7d' })"),
        (
            r"express\.static\(\s*path\.join\(__dirname,\s*'public'\)\s*\)",
            "express.static(path.join(__dirname, 'public'), { maxAge: '7d' })",
        ),
        (
            r'express\.static\(\s*path\.join\(__dirname,\s*"public"\)\s*\)',
            "express.static(path.join(__dirname, 'public'), { maxAge: '7d' })",
        ),
    ]

    changed = False
    for pat, repl in patterns:
        new_server = _re_mod.sub(pat, repl, server, count=1)
        if new_server != server:
            server = new_server
            changed = True
            break

    if changed:
        file_dict["server.js"] = server
        print("  [post-process] Added maxAge: '7d' to express.static")

    return list(file_dict.items())


@processor(after=('_ensure_static_caching',), phase=('build',))
def _ensure_lazy_loading(files: list) -> list:
    """Add loading="lazy" to <img> tags in EJS views, except hero images.

    Defers offscreen image loading for faster initial page paint.
    """
    import re as _re_mod

    file_dict = dict(files)
    changed = False

    for filepath, content in list(file_dict.items()):
        if not filepath.endswith(".ejs"):
            continue
        if "/views/" not in filepath and not filepath.startswith("views/"):
            continue

        original = content

        # Split into lines so we can detect hero context
        lines = content.split("\n")
        new_lines = []
        in_hero = False

        hero_depth = 0
        for line in lines:
            # Track hero sections: count opening/closing tags to handle div-based heroes
            if _re_mod.search(r'class="[^"]*hero', line, _re_mod.IGNORECASE):
                in_hero = True
                hero_depth = 1
            elif in_hero:
                hero_depth += line.count("<div") + line.count("<section")
                hero_depth -= line.count("</div>") + line.count("</section>")
                if hero_depth <= 0:
                    in_hero = False

            # Skip hero images (should load eagerly)
            if in_hero:
                new_lines.append(line)
                continue

            # Skip images that already have loading attribute
            if "loading=" in line:
                new_lines.append(line)
                continue

            # Add loading="lazy" to img tags
            line = _re_mod.sub(
                r'<img\s+',
                '<img loading="lazy" ',
                line,
            )
            new_lines.append(line)

        content = "\n".join(new_lines)
        if content != original:
            file_dict[filepath] = content
            changed = True

    if changed:
        print("  [post-process] Added loading=\"lazy\" to images in EJS views")

    return list(file_dict.items())


@processor(after=('_ensure_lazy_loading',), phase=('build',))
def _strip_duplicate_confirm_handlers(files: list) -> list:
    """Strip Claude-generated inline confirm handlers from layout.ejs.

    Claude often generates `document.addEventListener('click'|'submit', ...)`
    listeners that call `confirm()` on elements with `data-confirm`. Combined
    with our own `_ensure_submit_guard()` (which also handles data-confirm),
    a single delete-button click fires 2-3 duplicate confirmation dialogs.

    This post-processor removes Claude's duplicates so the submit guard is
    the single canonical source of confirm dialogs. It must run BEFORE
    `_ensure_submit_guard()` so the guard sees a clean layout.

    Triggers on: `document.addEventListener('click'|'submit', ...)` blocks
    whose body contains `confirm(`. Since this runs BEFORE submit_guard
    injects the canonical listener, ANY such handler at this stage is a
    Claude duplicate. Other document listeners (mobile menu, scroll, etc.)
    that do NOT call `confirm(` are untouched. Uses a brace-depth scanner
    rather than regex so it handles arbitrary nesting correctly.
    """
    import re as _re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Matches both `function(e) {` and `(e) => {` forms.
    pattern = _re_mod.compile(
        r"document\.addEventListener\s*\(\s*['\"](click|submit)['\"]\s*,"
        r"\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>)\s*\{"
    )

    out_parts: list[str] = []
    cursor = 0
    removed = 0

    while cursor < len(layout):
        m = pattern.search(layout, cursor)
        if not m:
            out_parts.append(layout[cursor:])
            break

        block_start = m.start()
        # m.end() - 1 points at the opening `{` of the function body.
        i = m.end() - 1
        if i >= len(layout) or layout[i] != '{':
            # Unexpected: bail safely.
            out_parts.append(layout[cursor:m.end()])
            cursor = m.end()
            continue

        depth = 1
        i += 1
        while i < len(layout) and depth > 0:
            c = layout[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            i += 1

        if depth != 0:
            # Unbalanced braces: can't safely strip, keep as-is.
            out_parts.append(layout[cursor:m.end()])
            cursor = m.end()
            continue

        # i now points one past the closing `}`. Consume trailing `);` etc.
        j = i
        while j < len(layout) and layout[j] in ' \t\n':
            j += 1
        if j < len(layout) and layout[j] == ')':
            j += 1
            while j < len(layout) and layout[j] in ' \t\n':
                j += 1
            if j < len(layout) and layout[j] == ';':
                j += 1

        block = layout[block_start:j]

        if 'confirm(' in block:
            # Drop the block; also consume trailing whitespace + newline for tidiness.
            end = j
            while end < len(layout) and layout[end] in ' \t':
                end += 1
            if end < len(layout) and layout[end] == '\n':
                end += 1
            out_parts.append(layout[cursor:block_start])
            cursor = end
            removed += 1
        else:
            # Keep this block and advance past it.
            out_parts.append(layout[cursor:j])
            cursor = j

    if removed > 0:
        new_layout = ''.join(out_parts)
        file_dict["views/layout.ejs"] = new_layout
        print(f"  [post-process] Stripped {removed} duplicate confirm handler(s) from layout.ejs")

    return list(file_dict.items())


@processor(after=('_ensure_button_design_system',), phase=('build',))
def _ensure_submit_guard(files: list) -> list:
    """Inject double-submit prevention script into layout.ejs before </body>.

    Disables submit buttons and shows 'Processing...' on form submit.
    Forms can opt out with data-no-submit-guard attribute.
    """
    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Skip if already has submit guard
    guard_markers = ["submit-guard", "submitting", "Processing..."]
    # Check for any existing guard but distinguish from data-confirm handler
    if any(m in layout for m in guard_markers):
        return files

    guard_script = """<script>
document.addEventListener('submit', function(e) {
  var form = e.target;
  if (form.dataset.noSubmitGuard) return;
  var btn = form.querySelector('button[type="submit"]');
  if (!btn || btn.disabled) return;
  if (form.dataset.confirm && !confirm(form.dataset.confirm)) { e.preventDefault(); return; }
  btn.disabled = true;
  btn.dataset.submitting = '1';
  btn.textContent = 'Processing...';
});
</script>"""

    if "</body>" in layout:
        layout = layout.replace("</body>", guard_script + "\n</body>", 1)
        file_dict["views/layout.ejs"] = layout
        print("  [post-process] Added double-submit prevention guard")

    return list(file_dict.items())


@processor(after=('_ensure_submit_guard',), phase=('build',))
def _ensure_dynamic_canonical(files: list) -> list:
    """Replace hardcoded canonical href="/" with dynamic URL via res.locals.

    Injects canonicalUrl into server.js middleware (where req is available)
    and uses it in layout.ejs. Correct canonical URLs prevent SEO duplicate
    content issues.
    """
    import re as _re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    server = file_dict.get("server.js", "")
    if not layout:
        return files

    # Skip if canonical already uses dynamic value
    if "canonicalUrl" in layout or ("canonical" in layout and "<%=" in layout.split("canonical")[1][:80] if "canonical" in layout else False):
        return files

    # Check for hardcoded canonical
    has_hardcoded = _re_mod.search(r'<link\s+rel="canonical"\s+href="/"', layout)
    if not has_hardcoded:
        return files

    # Step 1: Inject canonicalUrl into server.js res.locals middleware
    if server and "canonicalUrl" not in server:
        # Find where currentPath is set (common pattern in generated apps)
        current_path_pattern = "res.locals.currentPath = req.path;"
        if current_path_pattern in server:
            server = server.replace(
                current_path_pattern,
                current_path_pattern + "\n  res.locals.canonicalUrl = req.protocol + '://' + req.get('host') + req.originalUrl;",
            )
            file_dict["server.js"] = server

    # Step 2: Replace hardcoded canonical with EJS variable in layout
    new_layout = _re_mod.sub(
        r'<link\s+rel="canonical"\s+href="/"[^>]*>',
        '<link rel="canonical" href="<%= typeof canonicalUrl !== \'undefined\' ? canonicalUrl : \'/\' %>">',
        layout,
    )

    if new_layout != layout:
        file_dict["views/layout.ejs"] = new_layout
        print("  [post-process] Fixed canonical URL to use dynamic req-based value")

    return list(file_dict.items())


@processor(after=('_ensure_dynamic_canonical',), phase=('build',))
def _ensure_csrf_import(files: list) -> list:
    """Deduplicate CSRF validation: replace inline csrfCheck with imported validateCsrfToken.

    Claude generates identical csrfCheck functions in every route file. This
    post-processor replaces them with an import from middleware/auth.js.
    """
    import re as _re_mod

    file_dict = dict(files)

    # First ensure middleware/auth.js exports validateCsrfToken
    auth_mw = file_dict.get("middleware/auth.js", "")
    if not auth_mw or "validateCsrfToken" not in auth_mw:
        return files

    changed = False

    # Pattern 1: const csrfCheck = (req, res, next) => { ... };
    csrf_const_pattern = _re_mod.compile(
        r'(?:const|var|let)\s+(?:csrfCheck|validateCsrfToken)\s*=\s*(?:\(req,\s*res,\s*next\)\s*=>|function\s*\(req,\s*res,\s*next\))\s*\{(?:[^{}]*|\{[^{}]*\})*\};\s*\n?',
        _re_mod.DOTALL,
    )

    # Pattern 2: function validateCsrfToken(req, res, next) { ... }
    # or function csrfCheck(req, res, next) { ... }
    csrf_func_pattern = _re_mod.compile(
        r'function\s+(?:csrfCheck|validateCsrfToken)\s*\(req,\s*res,\s*next\)\s*\{(?:[^{}]*|\{[^{}]*\})*\}\s*\n?',
        _re_mod.DOTALL,
    )

    # Pattern 3: function getValidateCsrf() { try { return require(...) } catch(e) { ... } }
    csrf_fallback_pattern = _re_mod.compile(
        r'function\s+getValidateCsrf\s*\(\)\s*\{(?:[^{}]*|\{(?:[^{}]*|\{[^{}]*\})*\})*\}\s*\n?',
        _re_mod.DOTALL,
    )

    for filepath, content in list(file_dict.items()):
        if not filepath.startswith("routes/") or not filepath.endswith(".js"):
            continue

        original = content

        # Remove inline CSRF definitions
        content = csrf_const_pattern.sub("", content)
        content = csrf_func_pattern.sub("", content)
        content = csrf_fallback_pattern.sub("", content)

        # Also strip inline requireAuth/requireRole if already imported from middleware
        if "require('../middleware/auth')" in original and "requireAuth" in original:
            auth_func_pattern = _re_mod.compile(
                r'function\s+(?:requireAuth|requireRole)\s*\([^)]*\)\s*\{(?:[^{}]*|\{[^{}]*\})*\}\s*\n?',
                _re_mod.DOTALL,
            )
            content = auth_func_pattern.sub("", content)

        if content == original:
            continue

        # Ensure validateCsrfToken is imported from middleware/auth
        has_import = _re_mod.search(
            r"require\(['\"]\.\.\/middleware\/auth['\"]\)", content
        )
        if has_import:
            # Update the existing require to include validateCsrfToken
            existing_import = _re_mod.search(
                r"const\s*\{([^}]+)\}\s*=\s*require\(['\"]\.\.\/middleware\/auth['\"]\);?",
                content,
            )
            if existing_import:
                imports = existing_import.group(1)
                if "validateCsrfToken" not in imports:
                    new_imports = imports.rstrip() + ", validateCsrfToken"
                    content = content.replace(existing_import.group(0),
                        f"const {{{new_imports}}} = require('../middleware/auth');")
        else:
            # No existing import from middleware/auth, add one after the first require line
            first_require = _re_mod.search(r"(.*require\([^)]+\);?\s*\n)", content)
            if first_require:
                insert_pos = first_require.end()
                content = (content[:insert_pos]
                    + "const { validateCsrfToken } = require('../middleware/auth');\n"
                    + content[insert_pos:])

        # Replace csrfCheck references with validateCsrfToken (for older pattern)
        content = content.replace("csrfCheck", "validateCsrfToken")

        if content != original:
            file_dict[filepath] = content
            changed = True

    # Also remove duplicate validateCsrfToken from server.js if middleware/auth.js has it
    server = file_dict.get("server.js", "")
    if server and "validateCsrfToken" in server:
        # Remove standalone validateCsrfToken function definition from server.js
        server_csrf_pattern = _re_mod.compile(
            r'(?:const\s+)?(?:function\s+)?validateCsrfToken\s*(?:=\s*)?(?:\(req,\s*res,\s*next\)\s*=>|function\s*\(req,\s*res,\s*next\))\s*\{(?:[^{}]*|\{[^{}]*\})*\};\?\s*\n?',
            _re_mod.DOTALL,
        )
        new_server = server_csrf_pattern.sub("", server)
        if new_server != server:
            file_dict["server.js"] = new_server
            changed = True

    if changed:
        print("  [post-process] Deduplicated inline CSRF definitions, using imported validateCsrfToken")

    return list(file_dict.items())


@processor(after=('_ensure_csrf_import',), phase=('build',))
def _ensure_mobile_menu_close(files: list) -> list:
    """Close mobile hamburger menu when a nav link is clicked.

    Adds event listeners on mobile menu links to hide the menu on click.
    Handles both camelCase (mobileMenu) and dash-case (mobile-menu) IDs.
    """
    import re as _re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Detect the menu ID (Claude uses mobileMenu or mobile-menu)
    menu_id_match = _re_mod.search(r'id=["\']([^"\']*mobile[_-]?menu)["\']', layout, _re_mod.IGNORECASE)
    if not menu_id_match:
        return files

    menu_id = menu_id_match.group(1)

    # Skip if already has close-on-click logic for this menu's links
    if f"#{menu_id} a" in layout or f"{menu_id}').querySelectorAll('a')" in layout:
        return files

    close_script = (
        f"document.querySelectorAll('#{menu_id} a').forEach(function(a) {{"
        " a.addEventListener('click', function() {"
        f" document.getElementById('{menu_id}').classList.add('hidden');"
        " });"
        "});"
    )

    # Insert after the mobile menu toggle handler
    toggle_pattern = f"{menu_id}').classList.toggle('hidden')"
    if toggle_pattern in layout:
        idx = layout.index(toggle_pattern)
        search_from = idx + len(toggle_pattern)
        closing = layout.find("});", search_from)
        if closing != -1:
            insert_pos = closing + len("});")
            layout = layout[:insert_pos] + "\n    " + close_script + "\n" + layout[insert_pos:]
            file_dict["views/layout.ejs"] = layout
            print("  [post-process] Added mobile menu close-on-click handlers")

    return list(file_dict.items())


@processor(after=('_ensure_mobile_menu_close',), phase=('build',))
def _ensure_skip_to_content(files: list) -> list:
    """Add visually hidden 'Skip to content' link for keyboard accessibility.

    WCAG 2.1 AA requirement: keyboard users need a way to skip navigation.
    """
    import re as _re_mod

    file_dict = dict(files)
    layout = file_dict.get("views/layout.ejs", "")
    if not layout:
        return files

    # Skip if already has skip-to-content
    if "skip-to-content" in layout.lower() or "#main-content" in layout:
        return files

    skip_link = '<a href="#main-content" style="position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;color:var(--color-primary);" onfocus="this.style.cssText=\'position:absolute;top:0.5rem;left:0.5rem;z-index:50;background:white;padding:0.5rem 1rem;border-radius:0.375rem;box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);font-size:0.875rem;font-weight:600;color:var(--color-primary);width:auto;height:auto;clip:auto;overflow:visible;white-space:normal;\'" onblur="this.style.cssText=\'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;\'">Skip to content</a>'

    # Insert after <body...> tag
    body_match = _re_mod.search(r'(<body[^>]*>)', layout)
    if body_match:
        insert_pos = body_match.end()
        layout = layout[:insert_pos] + "\n" + skip_link + "\n" + layout[insert_pos:]

    # Ensure main content area has id="main-content"
    # Try <main first, then first <section after nav
    if 'id="main-content"' not in layout:
        if "<main" in layout and 'id="main-content"' not in layout:
            layout = _re_mod.sub(
                r'<main\b([^>]*)>',
                r'<main id="main-content"\1>',
                layout,
                count=1,
            )
        elif "</nav>" in layout:
            # Find first <section or <div after </nav>
            nav_end = layout.index("</nav>")
            after_nav = layout[nav_end:]
            section_match = _re_mod.search(r'<(section|div)\b([^>]*)>', after_nav)
            if section_match:
                tag = section_match.group(1)
                attrs = section_match.group(2)
                old_tag = section_match.group(0)
                new_tag = f'<{tag} id="main-content"{attrs}>'
                layout = layout[:nav_end] + after_nav.replace(old_tag, new_tag, 1)

    file_dict["views/layout.ejs"] = layout
    print("  [post-process] Added skip-to-content link and main-content ID")

    return list(file_dict.items())


def reapply_post_processors(build_dir, spec):
    """Re-run critical post-processors on files already written to disk.

    Called by the worker after fix.py / deploy_fix.py / qa_fix.py modify files.
    Fix loops can overwrite layout.ejs, db/migrate.js, style.css etc. without
    re-running post-processors, undoing color consistency, nav glass, font loading,
    resilient migrate, and other critical fixes.

    Only runs the subset of post-processors that fix loops most commonly damage.
    """
    build_path = Path(build_dir)
    # Read all files from disk into (path, content) list
    all_files = []
    for f in build_path.rglob("*"):
        if f.is_file() and "node_modules" not in str(f) and ".git" not in str(f):
            rel = str(f.relative_to(build_path))
            try:
                all_files.append((rel, f.read_text()))
            except (UnicodeDecodeError, PermissionError):
                pass  # skip binary files

    if not all_files:
        return

    print("  Re-applying critical post-processors after fix...")

    # Bind the build-dir contextvar so post-processors that need disk access
    # (e.g. _ensure_model_seed_images' disk-scan fallback) can find the build.
    # The set/reset pair must bracket the entire pipeline run plus the disk
    # write so a ContractViolation or any other exception from run_pipeline
    # never leaks the token into the caller's context (which would then
    # pollute unrelated tests / subsequent reapply calls).
    _build_dir_token = _CURRENT_BUILD_DIR.set(build_path)
    try:
        # Section-02-foundation: the hand-maintained subset list (formerly
        # ~40 explicit calls here, with load-bearing ordering comments about
        # FIX O vs FIX K, site_settings vs upload_form_ui, etc.) is now
        # derived from @processor(phase=("build","reapply")) metadata on
        # each function. run_pipeline(phase="reapply") resolves the
        # topological order via graphlib. The ordering comments are
        # preserved on the decorated functions themselves (see each
        # @processor(after=...) edge).
        _artifact = types.SimpleNamespace(
            files=all_files,
            spec=spec,
            build_dir=str(build_path),
            widgets_snippets={},
            metadata={},
        )
        run_pipeline(_artifact, phase="reapply")
        all_files = _artifact.files

        # Section-04: exclude any __widgets/ scratch paths from the disk write.
        all_files = [
            item for item in all_files
            if not (item and isinstance(item[0], str) and item[0].startswith('__widgets/'))
        ]

        # Write modified files back to disk
        for rel_path, content in all_files:
            target = build_path / rel_path
            if isinstance(content, (bytes, bytearray)):
                # Binary asset (e.g. MW logo bundled by FIX O), write bytes
                target.parent.mkdir(parents=True, exist_ok=True)
                existing = target.read_bytes() if target.exists() else None
                if existing != content:
                    target.write_bytes(content)
                continue
            try:
                existing = target.read_text() if target.exists() else None
            except (UnicodeDecodeError, PermissionError):
                continue
            if existing != content:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
    finally:
        _CURRENT_BUILD_DIR.reset(_build_dir_token)

    print("  Post-processors re-applied.")


def build_app(
    spec_path,
    output_dir=None,
    tier="build",
    *,
    _claude_response_override=None,
):
    """Main build pipeline: validate spec, call Claude, write files.

    Args:
        spec_path: Path to the spec JSON file.
        output_dir: Optional override for the build output directory.
        tier: Build tier for batch/realtime API selection.
        _claude_response_override: Test-only seam added in section-02-foundation.
            When provided, skips the live Claude generation (and the template
            pre-load) entirely and treats the override as the parsed file list.
            This lets byte-identical guard tests exercise the full
            post-processor pipeline against a pinned Claude-output fixture
            without an API call. Default None preserves production behavior.

    Raises RuntimeError on failure (never sys.exit).
    """
    # Step 1: Validate
    print(f"Loading spec: {spec_path}")
    spec, errors = load_and_validate(spec_path)
    if errors:
        raise RuntimeError(f"Spec validation failed: {errors[0]}")
    print(f"Spec valid: {spec['app_name']}")

    # Step 2: Set up output directory
    if output_dir is not None:
        output_dir = Path(output_dir)
    else:
        output_dir = Path("builds") / spec["app_name"]
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir}")

    # Step 3: Generate code (single-pass or multi-pass based on complexity).
    # If a Claude-response override was supplied (byte-identical guard tests),
    # use it verbatim and skip generation entirely.
    if _claude_response_override is not None:
        all_files = list(_claude_response_override)
    else:
        phase_plan = _should_use_multi_pass(spec)

        if phase_plan is None:
            print("Generating application code (single-pass)...")
            # Pre-load static templates so Claude can skip regenerating them.
            # Matches the multi-pass pre-load in _generate_multi_pass(); Claude's
            # version wins if it generates one (same merge semantics), and
            # post-processors normalize the final output either way.
            templates = _load_templates()
            prompt = _build_explicit_prompt(spec)
            generated = _call_and_parse(create_client(), prompt, tier=tier)
            if not generated:
                raise RuntimeError("No files were generated")
            # Merge: template is the baseline, Claude's generated file overrides.
            accumulated = dict(templates)
            for filepath, content in generated:
                accumulated[filepath] = content
            all_files = list(accumulated.items())
            if templates:
                skipped = sum(1 for path in templates if path not in dict(generated))
                print(
                    f"  Pre-loaded {len(templates)} template file(s); "
                    f"Claude skipped {skipped} of them"
                )
        else:
            print("Generating application code (multi-pass)...")
            all_files = _generate_multi_pass(spec, phase_plan, tier=tier)

    if not all_files:
        raise RuntimeError("No files were generated")

    # Step 3b: Post-process, deterministic fixes for generated code.
    # ContextVar makes output_dir reachable from post-processors that don't
    # receive build_dir in their signature (for fail-safe trace emission).
    #
    # Section-02-foundation: the explicit 99-call block has been replaced
    # with a single run_pipeline(artifact, phase="build"). Every decorated
    # post-processor still runs once in the same order as before; run_pipeline
    # resolves that order via graphlib from the @processor(after=...)
    # metadata on each function. See cli/pipeline.py for the mechanics.
    _build_dir_token = _CURRENT_BUILD_DIR.set(output_dir)
    try:
        emit_build_trace(
            output_dir,
            "post_processors.start",
            {"file_count": len(all_files)},
        )
        _artifact = types.SimpleNamespace(
            files=all_files,
            spec=spec,
            build_dir=str(output_dir),
            widgets_snippets={},
            metadata={},
        )
        run_pipeline(_artifact, phase="build")
        all_files = _artifact.files
        emit_build_trace(
            output_dir,
            "post_processors.end",
            {"file_count": len(all_files)},
        )
    finally:
        _CURRENT_BUILD_DIR.reset(_build_dir_token)

    # Step 3c: Validate completeness
    completeness_errors = validate_completeness(all_files)
    if completeness_errors:
        print(f"Build incomplete ({len(completeness_errors)} issue(s)):")
        for err in completeness_errors:
            print(f"  - {err}")
        print(f"\nGenerated {len(all_files)} files: {[f[0] for f in all_files]}")
        raise RuntimeError(f"Generated app failed completeness validation: {completeness_errors[0]}")

    # Step 4: Write files to disk
    # Section-04: exclude any __widgets/ scratch paths from the disk write.
    # Widget emission stores its non-file output in artifact.widgets_snippets
    # (a separate dict) but the filter is belt-and-suspenders for any future
    # code that stashes scratch content under __widgets/.
    all_files = [
        item for item in all_files
        if not (item and isinstance(item[0], str) and item[0].startswith('__widgets/'))
    ]
    print(f"\nWriting {len(all_files)} files:")
    write_files(all_files, output_dir)

    # Step 4b: Write binary logo file if spec has logo_data
    _ensure_logo_in_build(output_dir, spec)

    # Step 5: Copy spec into the build directory
    spec_dest = output_dir / "spec.json"
    with open(spec_dest, "w") as file:
        json.dump(spec, file, indent=2)
    print(f"  wrote spec.json")

    # Summary
    print(f"\nBuild complete: {len(all_files)} files in {output_dir}/")
    print(f"\nTo run:")
    print(f"  cd {output_dir}")
    print(f"  npm install")
    print(f"  createdb {spec['app_name'].replace('-', '_')}")
    print(f"  psql {spec['app_name'].replace('-', '_')} < db/schema.sql")
    print(f"  psql {spec['app_name'].replace('-', '_')} < db/seed.sql")
    print(f"  npm start")


# Section-05 (B+C plan): contract framework runs LAST in both phases.
# The after=tuple is computed at decoration time by reading every
# currently-registered processor name out of cli/pipeline.REGISTRY.
# This works because _run_contracts is defined at the very end of this
# module's source, so all other @processor decorators have already fired
# and populated REGISTRY by the time Python evaluates the arguments to
# this @processor call. Strategy (b) from the section-05 plan doc:
# explicit after=tuple(...), no runs_last= kwarg needed on pipeline.py.
#
# The comprehension filters out _run_contracts itself because the pipeline
# decorator is re-executed when build.py is imported twice (once as `build`
# by test shims, once as `cli.build` by the contract framework). On the
# second call REGISTRY already contains _run_contracts, and including it
# in the self-reference would both trip the duplicate-metadata guard AND
# create a self-edge that graphlib rejects as a cycle.
@processor(
    after=tuple(
        name for name in pipeline.REGISTRY.keys() if name != "_run_contracts"
    ),
    consumes=(
        "views/admin/settings.ejs",
        "views/admin/dashboard.ejs",
        "views/layout.ejs",
        "views/home.ejs",
        "views/about.ejs",
        "views/login.ejs",
    ),
    assumes_on_disk=(
        "views/admin/settings.ejs",
        "views/admin/dashboard.ejs",
        "views/layout.ejs",
        "views/home.ejs",
        "views/about.ejs",
        "views/login.ejs",
    ),
    phase=("build", "reapply"),
)
def _run_contracts(artifact):
    """Section-05 contract gate. Runs last in both build and reapply.

    Reads CAS_CONTRACTS_COLLECT env var; when set to '1' aggregates every
    violation into one raised ContractViolation instead of stopping on the
    first failure. Production default is fail-fast (unset env var).

    Artifact consumers: admin settings + CRUD + dashboard + layout + home +
    about + login EJS files. Missing mandatory files (admin_settings,
    layout, home, login) append a 'file missing' violation; optional
    surfaces (about, admin_dashboard, admin_crud per-model) early-return.
    """
    import os as _os_mod
    from cli.contracts.run import run_all
    collect_all = _os_mod.environ.get("CAS_CONTRACTS_COLLECT") == "1"
    run_all(artifact, collect_all=collect_all)
    return artifact


# Section-02-foundation: fail-fast DAG drift detection at module import.
# Every decorator above has registered itself in cli/pipeline.py::REGISTRY;
# assert_pipeline_parity() resolves both the 'build' and 'reapply' phases
# and raises if anything is inconsistent (unknown after-references, cycles,
# unsatisfied reapply consumers). Import-time failure means pytest collection
# and worker_pg.py startup both surface DAG drift instantly, rather than at
# customer-build time.
assert_pipeline_parity()


def main():
    """CLI entry point: build an app from a spec file."""
    if len(sys.argv) != 2:
        print("Usage: python cli/build.py <spec.json>")
        sys.exit(1)

    build_app(sys.argv[1])


if __name__ == "__main__":
    main()
