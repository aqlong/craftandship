// Lighthouse route tests (CAS Harbor Phase 1).
//
// The lighthouse router is mounted in app.ts only when
// CAS_HARBOR_ENABLED='true' at module load time. Other test files import
// app.ts WITHOUT that flag set, which means the router cache hits an
// un-mounted version. To get a fresh-with-flag-set import, we use
// vi.resetModules() + dynamic import inside beforeAll.

import { describe, it, expect, vi, beforeEach, beforeAll, afterEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import bcrypt from 'bcrypt';
import type { Application } from 'express';

// Mock pool.query + pool.connect before app loads. The bypass route
// uses promoteTrialToKeeper which checks out a transactional client
// via pool.connect(); tests opt into it on a per-test basis.
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  const mockConnect = vi.fn();
  return {
    default: {
      query: mockQuery,
      connect: mockConnect,
      on: vi.fn(),
    },
  };
});

// Mock companion runtime so /turn doesn't fire real Anthropic calls.
vi.mock('../../src/companion/runtime', async () => {
  const actual = await vi.importActual<typeof import('../../src/companion/runtime')>(
    '../../src/companion/runtime'
  );
  return {
    ...actual,
    turn: vi.fn(),
  };
});

// Mock spec extraction so /lighthouse/build doesn't fire real Opus calls.
// Only the call is stood in for: the readiness helpers the routes use and
// the module's error class are the real ones.
vi.mock('../../src/services/spec_extraction', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/services/spec_extraction')>()),
  extractSpecFromConversation: vi.fn(),
}));

// Mock the shared dispatch service so self-serve route tests exercise the
// guardrails without a real Opus extraction. Keep the REAL HarborDispatchError
// class so the route's `instanceof` mapping works.
vi.mock('../../src/services/harbor_dispatch', async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return { ...actual, dispatchHarborTrialBuild: vi.fn() };
});

// Mock services that other parts of the app import; minimal shapes only.
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
  sendReviewReadyEmail: vi.fn().mockResolvedValue(undefined),
  sendReviewChangesRequestedEmail: vi.fn().mockResolvedValue(undefined),
  sendHarborMadePublicEmail: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));
vi.mock('../../src/services/stripe', () => ({
  createHarborSubscriptionCheckoutSession: vi.fn().mockResolvedValue('https://checkout.stripe.com/mock-sub-session'),
  cancelHarborSubscriptionAtPeriodEnd: vi.fn().mockResolvedValue({ id: 'sub_x', cancel_at_period_end: true }),
  HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS: 1900,
  HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS: 3900,
  HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS: 3900,
}));
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello! Describe your app.'),
  analyzeBuildFailure: vi.fn(),
}));

let app: Application;
let mockQuery: ReturnType<typeof vi.fn>;
let mockTurn: ReturnType<typeof vi.fn>;
let mockExtract: ReturnType<typeof vi.fn>;
let mockDispatch: ReturnType<typeof vi.fn>;

beforeAll(async () => {
  process.env.CAS_HARBOR_ENABLED = 'true';
  process.env.NODE_ENV = process.env.NODE_ENV || 'test';
  // Reset module graph so app.ts re-evaluates with our env var set.
  vi.resetModules();
  const appModule = await import('../../src/app');
  app = appModule.default;
  const poolModule = await import('../../src/db/index');
  mockQuery = (poolModule.default.query as unknown) as ReturnType<typeof vi.fn>;
  const runtimeModule = await import('../../src/companion/runtime');
  mockTurn = runtimeModule.turn as unknown as ReturnType<typeof vi.fn>;
  const specModule = await import('../../src/services/spec_extraction');
  mockExtract = specModule.extractSpecFromConversation as unknown as ReturnType<
    typeof vi.fn
  >;
  const dispatchModule = await import('../../src/services/harbor_dispatch');
  mockDispatch = dispatchModule.dispatchHarborTrialBuild as unknown as ReturnType<
    typeof vi.fn
  >;
});

const TRIAL_ID = '11111111-2222-3333-4444-555555555555';
const VALID_TOKEN = 'a'.repeat(64);

const ACTIVE_TRIAL_ROW = {
  trial_id: TRIAL_ID,
  cookie_token: VALID_TOKEN,
  companion_name: 'Haven',
  companion_assigned: 'Haven',
  started_at: new Date('2026-04-26T10:00:00Z'),
  expires_at: new Date('2099-04-26T10:00:00Z'),
  last_seen_at: new Date('2026-04-26T11:00:00Z'),
};

function setupTrialCookieMocks(rows = [ACTIVE_TRIAL_ROW]): void {
  // trial-cookie middleware queries the DB once on every /lighthouse/* request.
  mockQuery.mockResolvedValueOnce({ rows });
}

/**
 * Log in a supertest agent as a CAS account by posting to /login. The
 * caller chooses the email; pass `process.env.ADMIN_EMAIL` to make the
 * agent admin-authenticated. Used by the dev_trial override gate tests
 * since `req.session.customerEmail` is the only signal `applyDevTrialOverride`
 * checks for admin status. Fetches a CSRF token from the /login GET so
 * the POST passes `validateCsrfToken`.
 */
async function loginAsLighthouseAgent(email: string): Promise<supertest.Agent> {
  const agent = supertest.agent(app);
  // GET /login to mint a CSRF + cookie.
  const getRes = await agent.get('/login');
  const csrfMatch = getRes.text.match(/name="_csrf"\s+value="([^"]+)"/);
  const csrf = csrfMatch ? csrfMatch[1] : '';
  // Mock the customer SELECT for the POST.
  const hashed = await bcrypt.hash('lighthousepass', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'cust-1', email, name: 'Test', password: hashed, email_verified: true }],
  });
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email, password: 'lighthousepass' });
  return agent;
}

/**
 * Queue the two SQL responses the email-gate helper expects on every
 * POST /turn: turn count + trial_keepers.email_captured. Defaults are
 * "below threshold" + "no email captured" which both pass-through.
 */
function setupEmailGateMocks(opts: { turnCount?: number; emailCaptured?: string | null } = {}): void {
  mockQuery.mockResolvedValueOnce({ rows: [{ c: String(opts.turnCount ?? 0) }] });
  mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: opts.emailCaptured ?? null }] });
}

describe('GET /lighthouse', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockTurn.mockReset();
  });

  it('redirects to /lighthouse/table', async () => {
    setupTrialCookieMocks();
    const res = await request(app).get('/lighthouse');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/lighthouse/table');
  });
});

describe('SEO noindex on /lighthouse/* surfaces', () => {
  // The keeper-conversion funnel is intentionally not SEO-targeted. robots.txt
  // disallows the path for compliant crawlers; the meta tag is the
  // belt-and-suspenders for AI crawlers that ignore robots.txt and against
  // accidental indexation if a /lighthouse URL leaks externally. Each rendered
  // /lighthouse/* surface MUST ship the meta tag in <head>.
  beforeEach(() => {
    mockQuery.mockReset();
    mockTurn.mockReset();
  });

  it('GET /lighthouse/table emits <meta name="robots" content="noindex, nofollow">', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).toMatch(
      /<meta\s+name="robots"\s+content="noindex,\s*nofollow"\s*\/?>/i,
    );
  });

  it('exposes res.locals.noIndex=true for /lighthouse paths so layout-harbor renders the meta tag', async () => {
    // Belt-and-suspenders: any future Harbor-layout view rendered for a
    // /lighthouse path must inherit the noindex gate. The home page (rendered
    // through layout-harbor.ejs without a lighthouse path) must NOT carry the
    // tag; /lighthouse/table MUST. Together these prove the gate is
    // path-conditional, not blanket-on-the-layout.
    mockQuery.mockResolvedValue({ rows: [], rowCount: 0 });
    const homeRes = await request(app).get('/');
    expect(homeRes.status).toBe(200);
    expect(homeRes.text).not.toMatch(/<meta\s+name="robots"\s+content="noindex/i);

    mockQuery.mockReset();
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const lhRes = await request(app).get('/lighthouse/table');
    expect(lhRes.status).toBe(200);
    expect(lhRes.text).toMatch(/<meta\s+name="robots"\s+content="noindex/i);
  });
});

describe('GET /lighthouse/table', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockTurn.mockReset();
  });

  it('renders the chat UI with the deterministic opening when no turns exist', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory: no prior turns
    mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory side-panel SELECT
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).toContain('keeper');
    // Opening message: "Hi. I'm so glad you found your way here." EJS escapes
    // the apostrophe to &#39; so we match a substring that doesn't contain one.
    expect(res.text).toContain('so glad you found your way here');
    expect(res.text).toContain('what brings you here today');
  });

  it("renders prior turns when the keeper's table has been visited before", async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          turn_id: 't1',
          keeper_text: 'Hi River here',
          companion_text: 'Hi River. Tell me about the work.',
          rail_flags: [],
          created_at: new Date('2026-04-26T11:00:00Z'),
        },
      ],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory side-panel SELECT
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Hi River here');
    expect(res.text).toContain('Tell me about the work');
  });

  it('renders the CAS brand bar above the Haven header (logo + wordmark, opens in new tab)', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // Brand bar uses the canonical CAS lighthouse mark + wordmark and
    // links to the marketing site in a new tab so the keeper's
    // conversation never gets interrupted.
    expect(res.text).toMatch(
      /<a[^>]*class="cas-brand-bar"[^>]*href="\/"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/
    );
    expect(res.text).toContain('/brand-harbor/logo-mark-nav.svg');
    expect(res.text).toMatch(/<img[^>]*class="cas-brand-mark"[^>]*src="\/brand-harbor\/logo-mark-nav\.svg"/);
    expect(res.text).toMatch(/<span class="cas-brand-name">Craft <span class="ampersand">&amp;<\/span> Ship<\/span>/);
    // Brand bar must precede the Haven header in document order.
    const brandIndex = res.text.indexOf('cas-brand-bar');
    const headerIndex = res.text.indexOf('keepers-table-header');
    expect(brandIndex).toBeGreaterThan(0);
    expect(headerIndex).toBeGreaterThan(brandIndex);
    // The previous inline lighthouse SVG glyph is gone.
    expect(res.text).not.toMatch(/<svg\s+class="glyph"/);
  });

  // ─────────────────────────────────────────────────────────────────
  // a11y: visually-hidden labels for inputs without visible labels
  // (harbor-a11y-email-gate-input-label, 2026-05-09).
  //
  // Three customer-facing inputs on /lighthouse/table previously
  // shipped with placeholder-only "labels":
  //   1. compose textarea (every keeper sees, every page load)
  //   2. JS-rendered email-gate-input (every keeper at turn 22)
  //   3. dev-only bypass-email (admin / dev, lower impact)
  //
  // WCAG 2.1 AA §1.3.1 (Info and Relationships) + §3.3.2 (Labels or
  // Instructions) require an accessible name. Placeholder disappears
  // on focus, leaving a nameless field for screen-reader / voice-
  // control users.
  // ─────────────────────────────────────────────────────────────────

  it('compose textarea has visually-hidden label + matching aria-label', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // sr-only label for assistive-tech announcement.
    expect(res.text).toMatch(
      /<label\s+for="keeper-text"\s+class="cas-sr-only">Your message to Haven<\/label>/
    );
    // aria-label on the textarea itself as belt-and-suspenders for
    // browsers that don't honor sr-only labels (older Android TalkBack).
    expect(res.text).toMatch(
      /<textarea[\s\S]*?id="keeper-text"[\s\S]*?aria-label="Your message to Haven"/
    );
    // The .cas-sr-only utility class must exist in the page <style>.
    expect(res.text).toMatch(/\.cas-sr-only\s*\{[^}]*position:\s*absolute/);
  });

  it('JS-rendered email-gate form includes visually-hidden label + autocomplete', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // The appendEmailGate JS builds the form HTML from string literals;
    // assert the literal contains a label[for=email-gate-input] tag.
    expect(res.text).toContain(
      "<label for=\"email-gate-input\" class=\"cas-sr-only\">Your email</label>"
    );
    // And the input carries autocomplete="email" so password managers /
    // assistive tech can resolve it correctly.
    expect(res.text).toContain('autocomplete="email"');
    expect(res.text).toContain('aria-required="true"');
  });

  it('dev-only bypass-email input has visually-hidden label + autocomplete (when bypass enabled)', async () => {
    // Force the bypass affordance to render so we can scan the input.
    process.env.CAS_HARBOR_STRIPE_BYPASS = 'true';
    process.env.NODE_ENV = 'test'; // Anything except 'production'.
    try {
      setupTrialCookieMocks();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await request(app).get('/lighthouse/table');
      expect(res.status).toBe(200);
      expect(res.text).toMatch(
        /<label\s+for="bypass-email"\s+class="cas-sr-only">Your email<\/label>/
      );
      expect(res.text).toMatch(
        /<input[^>]*id="bypass-email"[^>]*autocomplete="email"/
      );
    } finally {
      delete process.env.CAS_HARBOR_STRIPE_BYPASS;
    }
  });

  it('puts the Haven bot avatar next to "Haven" in the keepers-table header', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // Haven header carries an avatar so it's visually clear the cute
    // bot painting IS the companion the keeper will be talking with.
    // The avatar is decorative (alt="") because the name next to it carries
    // the accessible name; the name is the page's <h1> (SR4/SR6).
    expect(res.text).toMatch(
      /<header class="keepers-table-header"[^>]*aria-labelledby="keepers-table-name keepers-table-eyebrow"[^>]*>[\s\S]*?<img class="haven-header-avatar"[^>]*src="\/brand-harbor\/haven-avatar-80\.png"[^>]*alt=""[\s\S]*?<h1 class="name" id="keepers-table-name">Haven<\/h1>/
    );
  });

  it('wraps each companion turn in a row with avatar + "Haven" name label', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          turn_id: 't1',
          keeper_text: 'hi',
          companion_text: 'first companion reply',
          rail_flags: [],
          created_at: new Date('2026-04-28T09:00:00Z'),
        },
        {
          turn_id: 't2',
          keeper_text: 'more',
          companion_text: 'second companion reply',
          rail_flags: [],
          created_at: new Date('2026-04-28T09:01:00Z'),
        },
      ],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // Two companion turns → two rows + two row-avatars + two "Haven" labels.
    const rowMatches = res.text.match(/class="turn-row turn-row-companion"/g) || [];
    const avatarMatches = res.text.match(/class="haven-avatar"/g) || [];
    const havenLabelMatches = res.text.match(/class="turn-name turn-name-companion">Haven</g) || [];
    expect(rowMatches.length).toBe(2);
    expect(avatarMatches.length).toBe(2);
    expect(havenLabelMatches.length).toBe(2);
    expect(res.text).toContain('/brand-harbor/haven-avatar-80.png');
    // Avatar precedes the stack which holds the label and bubble.
    expect(res.text).toMatch(
      /turn-row turn-row-companion[\s\S]*?haven-avatar[\s\S]*?turn-stack[\s\S]*?turn-name-companion[\s\S]*?turn-companion[\s\S]*?first companion reply/
    );
  });

  it('wraps each keeper turn in a "You"-labeled bubble', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          turn_id: 't1',
          keeper_text: 'first keeper line',
          companion_text: 'companion reply',
          rail_flags: [],
          created_at: new Date('2026-04-28T09:00:00Z'),
        },
        {
          turn_id: 't2',
          keeper_text: 'second keeper line',
          companion_text: 'second companion reply',
          rail_flags: [],
          created_at: new Date('2026-04-28T09:01:00Z'),
        },
      ],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // Two keeper turns → two wraps + two "You" labels.
    const wrapMatches = res.text.match(/class="turn-keeper-wrap"/g) || [];
    const youLabelMatches = res.text.match(/class="turn-name turn-name-keeper">You</g) || [];
    expect(wrapMatches.length).toBe(2);
    expect(youLabelMatches.length).toBe(2);
    // The "You" label sits inside the wrap and precedes the bubble.
    expect(res.text).toMatch(
      /turn-keeper-wrap[\s\S]*?turn-name-keeper">You<[\s\S]*?turn-keeper">first keeper line/
    );
  });

  it('renders the keeper compose textarea at the verbose-friendly size', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // Textarea defaults to 4 visible rows so the resting size is
    // generous enough that verbose keepers don't feel cramped.
    expect(res.text).toMatch(/<textarea[^>]*id="keeper-text"[^>]*rows="4"/);
    // CSS contract: min-height 96px floor + 480px ceiling so the
    // autosize JS has room to grow as the keeper writes a long draft.
    expect(res.text).toMatch(/\.keepers-input textarea \{[\s\S]*?min-height: 96px;[\s\S]*?max-height: 480px;/);
    // Autosize cap in JS must match the CSS ceiling so the JS height
    // stops growing exactly when the CSS overflow scroll kicks in.
    expect(res.text).toMatch(/Math\.min\(textarea\.scrollHeight, 480\)/);
    // The .keepers-table bottom padding accommodates the maximum
    // textarea height + chrome so the last turn isn't covered.
    expect(res.text).toMatch(/\.keepers-table \{[\s\S]*?padding: 56px 32px 580px;/);
  });

  it('opens "what Haven remembers" link in a new tab with safe rel attributes', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).toMatch(
      /<a\s+href="\/lighthouse\/table\/recall[^"]*"[^>]*target="_blank"[^>]*rel="noopener noreferrer"[^>]*class="quiet-link"[^>]*>what Haven remembers about you<\/a>/
    );
  });

  it('renders a "see how your site could look" link to /lighthouse/preview (new tab, safe rel)', async () => {
    // Phase 2 follow-up (2026-04-29): the table surface needs to
    // surface the preview picker so customers can discover it without
    // Haven manually pointing them at it. New tab so customers don't
    // lose their conversation.
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).toMatch(
      /<a\s+href="\/lighthouse\/preview[^"]*"[^>]*target="_blank"[^>]*rel="noopener noreferrer"[^>]*class="quiet-link"[^>]*>see how your site could look<\/a>/
    );
  });

  it('preview link uses devTrialQuery template variable (so admin override flows through)', async () => {
    // Verify the link template expression references devTrialQuery so
    // when the dev_trial query param IS present (admin override path),
    // it gets appended to the href. The full admin-auth flow is covered
    // by the broader devTrialQuery tests; this asserts the template
    // contract for the new preview link specifically.
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // No dev_trial in this request -> href is bare (no ?dev_trial=).
    // Passing this assertion means the template uses the same
    // devTrialQuery substitution as the recall link above it.
    expect(res.text).toMatch(
      /href="\/lighthouse\/preview"[^>]*>see how your site could look/
    );
  });

  it('client-side appenders mirror the server "Haven"/"You" labeled structure', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    // Companion appender: row + avatar + stack + Haven label.
    expect(res.text).toMatch(/row\.className\s*=\s*['"]turn-row turn-row-companion['"]/);
    expect(res.text).toMatch(/avatar\.className\s*=\s*['"]haven-avatar['"]/);
    expect(res.text).toMatch(/avatar\.src\s*=\s*['"]\/brand-harbor\/haven-avatar-80\.png['"]/);
    expect(res.text).toMatch(/stack\.className\s*=\s*['"]turn-stack['"]/);
    expect(res.text).toMatch(/label\.className\s*=\s*['"]turn-name turn-name-companion['"]/);
    // The companion name reaches the script through a data attribute, not by
    // interpolation into JavaScript source.
    expect(res.text).toMatch(/data-companion-name="Haven"/);
    expect(res.text).toMatch(/label\.textContent\s*=\s*companionName/);
    // Keeper appender: wrap + You label + bubble.
    expect(res.text).toMatch(/wrap\.className\s*=\s*['"]turn-keeper-wrap['"]/);
    expect(res.text).toMatch(/label\.className\s*=\s*['"]turn-name turn-name-keeper['"]/);
    expect(res.text).toMatch(/label\.textContent\s*=\s*['"]You['"]/);
  });

  it('honors ?dev_trial= override in non-production WHEN logged in as admin', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    const ORIGINAL_ADMIN = process.env.ADMIN_EMAIL;
    process.env.NODE_ENV = 'development';
    process.env.ADMIN_EMAIL = 'admin@example.com';
    try {
      // Admin login flow consumes 2 mockQuery calls (a GET /login render
      // does not query DB; the POST /login customer SELECT is seeded
      // inside the helper itself). After login the agent carries an
      // admin session cookie.
      const agent = await loginAsLighthouseAgent('admin@example.com');
      // Logged-in supertest agent has session.customerId set, which
      // triggers an EXTRA `SELECT handoff_build_id FROM keepers WHERE
      // customer_id = $1` lookup in the lighthouse table route to
      // detect post-promotion keeper status. Seed that extra mock too.
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // dev_trial override
      mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] }); // keepers.handoff_build_id (logged-in only)
      mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
      const res = await agent.get(`/lighthouse/table?dev_trial=${TRIAL_ID}`);
      expect(res.status).toBe(200);
      const overrideCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM trial_keepers') &&
          c[0].includes('WHERE trial_id = $1')
      );
      expect(overrideCall).toBeDefined();
      expect(overrideCall![1]).toEqual([TRIAL_ID]);
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      if (ORIGINAL_ADMIN === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = ORIGINAL_ADMIN;
    }
  });

  it('IGNORES ?dev_trial= override in non-production for unauthenticated visitors', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    const ORIGINAL_ADMIN = process.env.ADMIN_EMAIL;
    process.env.NODE_ENV = 'development';
    process.env.ADMIN_EMAIL = 'admin@example.com';
    try {
      // No login. supertest.agent will not carry a session.customerEmail.
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
      const res = await request(app).get(`/lighthouse/table?dev_trial=${TRIAL_ID}`);
      expect(res.status).toBe(200);
      // Override SQL must NOT have fired: only the trial-cookie middleware's
      // SELECT (different SQL shape) and loadTurnHistory should hit the DB.
      const overrideCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM trial_keepers') &&
          c[0].includes('WHERE trial_id = $1') &&
          // Filter to the override query specifically (it returns
          // companion_assigned + started_at + expires_at + last_seen_at).
          c[0].includes('companion_assigned')
      );
      expect(overrideCall).toBeUndefined();
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      if (ORIGINAL_ADMIN === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = ORIGINAL_ADMIN;
    }
  });

  it('IGNORES ?dev_trial= override for non-admin logged-in users', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    const ORIGINAL_ADMIN = process.env.ADMIN_EMAIL;
    process.env.NODE_ENV = 'development';
    process.env.ADMIN_EMAIL = 'admin@example.com';
    try {
      // Logged in but as a non-admin email. Override must still skip.
      const agent = await loginAsLighthouseAgent('regular@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] }); // keepers.handoff_build_id (logged-in only)
      mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
      const res = await agent.get(`/lighthouse/table?dev_trial=${TRIAL_ID}`);
      expect(res.status).toBe(200);
      const overrideCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM trial_keepers') &&
          c[0].includes('WHERE trial_id = $1') &&
          c[0].includes('companion_assigned')
      );
      expect(overrideCall).toBeUndefined();
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      if (ORIGINAL_ADMIN === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = ORIGINAL_ADMIN;
    }
  });

  it('rejects invalid UUID shape on dev_trial without firing the override SQL', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    try {
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
      const res = await request(app).get('/lighthouse/table?dev_trial=not-a-uuid');
      expect(res.status).toBe(200);
      // No override SQL fired for the malformed UUID. The override SQL
      // SELECTs from trial_keepers + matches by trial_id; loadTurnHistory
      // SELECTs from companion_turns. Filter by FROM clause to disambiguate.
      const overrideCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM trial_keepers') &&
          c[0].includes('WHERE trial_id = $1')
      );
      expect(overrideCall).toBeUndefined();
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });

  it('IGNORES dev_trial override in production', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'production';
    try {
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
      const res = await request(app).get(`/lighthouse/table?dev_trial=${TRIAL_ID}`);
      expect(res.status).toBe(200);
      // Match the dev_trial override SELECT specifically (it loads the full
      // trial context incl. cookie_token); not the self-serve eligibility
      // SELECT, which also reads trial_keepers by trial_id but selects
      // email_captured / is_synthetic, not cookie_token.
      const overrideCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM trial_keepers') &&
          c[0].includes('cookie_token')
      );
      expect(overrideCall).toBeUndefined();
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });
});

describe('POST /lighthouse/table/turn', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockTurn.mockReset();
  });

  it('rejects empty keeperText with 400', async () => {
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: '' });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/keeperText is required/);
  });

  it('rejects missing keeperText body field with 400', async () => {
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({});
    expect(res.status).toBe(400);
  });

  it('rejects keeperText > 4000 chars with 413', async () => {
    setupTrialCookieMocks();
    const tooLong = 'a'.repeat(4001);
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: tooLong });
    expect(res.status).toBe(413);
    expect(res.body.error).toMatch(/too long/);
  });

  it('happy path: invokes runtime.turn() + returns companion reply', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 3, emailCaptured: null }); // below threshold
    mockTurn.mockResolvedValueOnce({
      companionText: 'Tell me about the work.',
      turnId: 'turn-99',
      railFlags: [],
      costMicros: 1234,
    });
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'I run a yoga studio.' });
    expect(res.status).toBe(200);
    // The per-turn cost is internal: recorded server-side, not sent to the visitor.
    expect(res.body).toEqual({
      companionText: 'Tell me about the work.',
      turnId: 'turn-99',
      railFlags: [],
    });
    expect(mockTurn).toHaveBeenCalledOnce();
    const turnArgs = mockTurn.mock.calls[0][0];
    expect(turnArgs.trialId).toBe(TRIAL_ID);
    expect(turnArgs.keeperText).toBe('I run a yoga studio.');
  });

  it('email gate: returns 402 with requiresEmail when turn count >= threshold and no email captured', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 22, emailCaptured: null });
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'continue' });
    expect(res.status).toBe(402);
    expect(res.body.requiresEmail).toBe(true);
    expect(res.body.turnCount).toBe(22);
    expect(res.body.threshold).toBe(22);
    expect(mockTurn).not.toHaveBeenCalled();
  });

  it('email gate: turn proceeds when keeper provides email at threshold (saves + processes)', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 22, emailCaptured: null });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // UPDATE trial_keepers email_captured
    mockTurn.mockResolvedValueOnce({
      companionText: 'OK, glad to keep going.',
      turnId: 'turn-100',
      railFlags: [],
      costMicros: 800,
    });
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'continue', email: 'River@Example.COM' });
    expect(res.status).toBe(200);
    expect(res.body.companionText).toBe('OK, glad to keep going.');
    // UPDATE was called with the lowercased email.
    const updateCall = mockQuery.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('SET email_captured')
    );
    expect(updateCall).toBeDefined();
    expect((updateCall![1] as unknown[])[0]).toBe('river@example.com');
  });

  it('email gate: 400 + invalid email when keeper sends a malformed email at threshold', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 25, emailCaptured: null });
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'hi', email: 'not-an-email' });
    expect(res.status).toBe(400);
    expect(res.body.requiresEmail).toBe(true);
    expect(mockTurn).not.toHaveBeenCalled();
  });

  it('email gate: passes when email already captured even at threshold', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 50, emailCaptured: 'river@example.com' });
    mockTurn.mockResolvedValueOnce({
      companionText: 'continuing',
      turnId: 't',
      railFlags: [],
      costMicros: 100,
    });
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'go on' });
    expect(res.status).toBe(200);
    expect(mockTurn).toHaveBeenCalledOnce();
  });

  it('email gate: respects CAS_HARBOR_EMAIL_GATE_TURN_COUNT env override', async () => {
    const ORIGINAL = process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT;
    process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT = '5';
    try {
      setupTrialCookieMocks();
      setupEmailGateMocks({ turnCount: 5, emailCaptured: null });
      const res = await request(app)
        .post('/lighthouse/table/turn')
        .set('Content-Type', 'application/json')
        .send({ keeperText: 'hi' });
      expect(res.status).toBe(402);
      expect(res.body.threshold).toBe(5);
    } finally {
      if (ORIGINAL === undefined) delete process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT;
      else process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT = ORIGINAL;
    }
  });

  it('trims keeperText whitespace before validation', async () => {
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: '   ' });
    expect(res.status).toBe(400);
  });

  it('does NOT honor dev_trial override on POST (cookie identity is authoritative for state-changing calls)', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    try {
      setupTrialCookieMocks();
      setupEmailGateMocks({ turnCount: 1, emailCaptured: null });
      mockTurn.mockResolvedValueOnce({
        companionText: 'ok',
        turnId: 't',
        railFlags: [],
        costMicros: 0,
      });
      await request(app)
        .post(`/lighthouse/table/turn?dev_trial=${TRIAL_ID}`)
        .set('Content-Type', 'application/json')
        .send({ keeperText: 'hi' });
      // No override-style SELECT against trial_keepers WHERE trial_id = $1
      // that doesn't reference cookie_token AND email_captured.
      const overrideCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM trial_keepers') &&
          c[0].includes('trial_id = $1') &&
          !c[0].includes('cookie_token') &&
          !c[0].includes('email_captured')
      );
      expect(overrideCall).toBeUndefined();
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });

  it('forwards runtime errors via the express error handler (500)', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 1, emailCaptured: null });
    mockTurn.mockRejectedValueOnce(new Error('Anthropic 503'));
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'hi' });
    expect(res.status).toBe(500);
  });

  // ─────────────────────────────────────────────────────────────────
  // Daily cost cap (harbor-companion-turn-rate-limit-and-cost-cap, 2026-05-09)
  //
  // Runtime.turn() throws CompanionDailyCostCapError when this trial /
  // keeper has burned its 24h Anthropic budget. Route catches the typed
  // error and returns 429 with `error: daily_cost_cap_exceeded` plus
  // current/cap micros: never propagates to the 500 path.
  // ─────────────────────────────────────────────────────────────────

  it('cost cap: 429 daily_cost_cap_exceeded when runtime throws CompanionDailyCostCapError', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 3, emailCaptured: 'r@example.com' });
    const runtimeModule = await import('../../src/companion/runtime');
    const err = new runtimeModule.CompanionDailyCostCapError(5_500_000, 5_000_000);
    mockTurn.mockRejectedValueOnce(err);
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'hi' });
    expect(res.status).toBe(429);
    expect(res.body.error).toBe('daily_cost_cap_exceeded');
    // Spend figures stay in the server log.
    expect(res.body).not.toHaveProperty('currentMicros');
    expect(res.body).not.toHaveProperty('capMicros');
    expect(JSON.stringify(res.body)).not.toMatch(/5[_,.]?5/);
    // Friendly message, not stack trace.
    expect(res.body.message).toMatch(/come back tomorrow/i);
  });

  it('no model key: 503 assistant_not_configured with a plain explanation, not a 500 page', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 3, emailCaptured: 'r@example.com' });
    const { AssistantNotConfiguredError } = await import('../../src/utils/assistant');
    mockTurn.mockRejectedValueOnce(new AssistantNotConfiguredError('claude: ANTHROPIC_API_KEY is not set'));
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'hi' });
    expect(res.status).toBe(503);
    expect(res.body.code).toBe('assistant_not_configured');
    expect(res.body.message).toMatch(/assistant is not configured on this server/i);
    // The technical message of the thrown error is for logs; the client gets the human sentence.
    expect(JSON.stringify(res.body)).not.toContain('claude:');
  });

  it('cost cap error is distinct from 500 path (not propagated to default error handler)', async () => {
    setupTrialCookieMocks();
    setupEmailGateMocks({ turnCount: 3, emailCaptured: 'r@example.com' });
    const runtimeModule = await import('../../src/companion/runtime');
    mockTurn.mockRejectedValueOnce(
      new runtimeModule.CompanionDailyCostCapError(10_000_000, 5_000_000)
    );
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Content-Type', 'application/json')
      .send({ keeperText: 'over budget' });
    expect(res.status).not.toBe(500);
    expect(res.status).toBe(429);
  });
});

// ----------------------------------------------------------------------
// POST /lighthouse/bypass: local-only Stripe bypass for dev/test.
// ----------------------------------------------------------------------

describe('POST /lighthouse/bypass (local-only Stripe skip)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockExtract.mockReset();
  });

  it('returns 403 when CAS_HARBOR_STRIPE_BYPASS is unset', async () => {
    const ORIGINAL = process.env.CAS_HARBOR_STRIPE_BYPASS;
    delete process.env.CAS_HARBOR_STRIPE_BYPASS;
    try {
      setupTrialCookieMocks();
      const res = await request(app)
        .post('/lighthouse/bypass')
        .set('Content-Type', 'application/json')
        .send({ email: 'river@example.com' });
      expect(res.status).toBe(403);
      expect(res.body.error).toBe('bypass_disabled');
    } finally {
      if (ORIGINAL === undefined) delete process.env.CAS_HARBOR_STRIPE_BYPASS;
      else process.env.CAS_HARBOR_STRIPE_BYPASS = ORIGINAL;
    }
  });

  it('returns 403 when NODE_ENV=production even with the flag set', async () => {
    const ORIGINAL_FLAG = process.env.CAS_HARBOR_STRIPE_BYPASS;
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.CAS_HARBOR_STRIPE_BYPASS = 'true';
    process.env.NODE_ENV = 'production';
    try {
      setupTrialCookieMocks();
      const res = await request(app)
        .post('/lighthouse/bypass')
        .set('Content-Type', 'application/json')
        .send({ email: 'river@example.com' });
      expect(res.status).toBe(403);
      expect(res.body.error).toBe('bypass_blocked_in_production');
    } finally {
      if (ORIGINAL_FLAG === undefined) delete process.env.CAS_HARBOR_STRIPE_BYPASS;
      else process.env.CAS_HARBOR_STRIPE_BYPASS = ORIGINAL_FLAG;
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });

  it('returns 400 when email is missing or malformed', async () => {
    const ORIGINAL_FLAG = process.env.CAS_HARBOR_STRIPE_BYPASS;
    process.env.CAS_HARBOR_STRIPE_BYPASS = 'true';
    try {
      setupTrialCookieMocks();
      const r1 = await request(app)
        .post('/lighthouse/bypass')
        .set('Content-Type', 'application/json')
        .send({});
      expect(r1.status).toBe(400);

      setupTrialCookieMocks();
      const r2 = await request(app)
        .post('/lighthouse/bypass')
        .set('Content-Type', 'application/json')
        .send({ email: 'not-an-email' });
      expect(r2.status).toBe(400);
    } finally {
      if (ORIGINAL_FLAG === undefined) delete process.env.CAS_HARBOR_STRIPE_BYPASS;
      else process.env.CAS_HARBOR_STRIPE_BYPASS = ORIGINAL_FLAG;
    }
  });
});

// ----------------------------------------------------------------------
// POST /lighthouse/bypass must never be a way INTO an existing account,
// and must be inert outside development and test.
// ----------------------------------------------------------------------

describe('POST /lighthouse/bypass (account takeover and environment gates)', () => {
  let mockConnect: ReturnType<typeof vi.fn>;
  const saved: Record<string, string | undefined> = {};

  beforeAll(async () => {
    const poolModule = await import('../../src/db/index');
    mockConnect = (poolModule.default.connect as unknown) as ReturnType<typeof vi.fn>;
  });

  beforeEach(() => {
    mockQuery.mockReset();
    mockConnect.mockReset();
    for (const key of ['CAS_HARBOR_STRIPE_BYPASS', 'NODE_ENV', 'ADMIN_EMAIL']) saved[key] = process.env[key];
    process.env.CAS_HARBOR_STRIPE_BYPASS = 'true';
    process.env.NODE_ENV = 'test';
    process.env.ADMIN_EMAIL = 'admin@example.com';
  });

  afterEach(() => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  /**
   * A pg client double that satisfies promoteTrialToKeeper's transaction. The
   * keeper claim writes the row unless the account's keeper already has a
   * subscription of its own (`keeperHasSubscription`), when it matches nothing.
   */
  function promotionClient(
    opts: { keeperHasSubscription?: boolean } = {}
  ): { query: ReturnType<typeof vi.fn>; release: ReturnType<typeof vi.fn> } {
    return {
      query: vi.fn(async (sql: string) => {
        if (/FROM trial_keepers[\s\S]*FOR UPDATE/.test(sql)) {
          return {
            rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
            rowCount: 1,
          };
        }
        if (/INSERT INTO keepers[\s\S]*RETURNING customer_id/.test(sql)) {
          return opts.keeperHasSubscription
            ? { rows: [], rowCount: 0 }
            : { rows: [{ customer_id: 'cust-1' }], rowCount: 1 };
        }
        return { rows: [], rowCount: 0 };
      }),
      release: vi.fn(),
    };
  }

  it('refuses an email that belongs to an existing customer, and does not log the caller in', async () => {
    const agent = supertest.agent(app);
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-victim' }] }); // SELECT customers by email
    const res = await agent
      .post('/lighthouse/bypass')
      .set('Content-Type', 'application/json')
      .send({ email: 'Victim@Example.com' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('account_exists');
    // No promotion transaction was opened and nothing was written.
    expect(mockConnect).not.toHaveBeenCalled();
    expect(mockQuery.mock.calls.some(([sql]) => /INSERT INTO customers/.test(String(sql)))).toBe(false);
    // The session is still anonymous: a customer page sends it to /login.
    const dashboard = await agent.get('/dashboard');
    expect(dashboard.status).toBe(302);
    expect(dashboard.headers.location).toMatch(/^\/login/);
  });

  it('lets a logged-in customer run the bypass for their own account', async () => {
    const agent = await loginAsLighthouseAgent('river@example.com'); // session customerId = cust-1
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] }); // SELECT customers by email
    mockConnect.mockResolvedValueOnce(promotionClient());
    const res = await agent
      .post('/lighthouse/bypass')
      .set('Content-Type', 'application/json')
      .send({ email: 'river@example.com' });
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ bypass: true, promoted: true, customerId: 'cust-1' });
  });

  it('refuses to promote a trial into an account whose keeper already has a subscription: 409, and the session is not touched', async () => {
    const agent = await loginAsLighthouseAgent('river@example.com'); // session customerId = cust-1
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] }); // SELECT customers by email
    const client = promotionClient({ keeperHasSubscription: true });
    mockConnect.mockResolvedValueOnce(client);
    const res = await agent
      .post('/lighthouse/bypass')
      .set('Content-Type', 'application/json')
      .send({ email: 'river@example.com' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('account_already_subscribed');
    // The claim was the last statement the promotion issued before the commit: nothing was moved or released.
    const sqls = client.query.mock.calls.map(([sql]) => String(sql));
    expect(sqls.some((sql) => /UPDATE (companion_turns|keeper_memory|trial_keepers)/.test(sql))).toBe(false);
  });

  it('refuses a logged-in customer who names a different existing account', async () => {
    const agent = await loginAsLighthouseAgent('river@example.com'); // cust-1
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-someone-else' }] });
    const res = await agent
      .post('/lighthouse/bypass')
      .set('Content-Type', 'application/json')
      .send({ email: 'someone-else@example.com' });
    expect(res.status).toBe(409);
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it('creates a fresh account for an email nobody has registered', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // SELECT customers: none
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-new' }] }); // INSERT customers RETURNING id
    mockConnect.mockResolvedValueOnce(promotionClient());
    const res = await request(app)
      .post('/lighthouse/bypass')
      .set('Content-Type', 'application/json')
      .send({ email: 'fresh@example.com' });
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ bypass: true, promoted: true, customerId: 'cust-new' });
  });

  it('refuses ADMIN_EMAIL for a session that is not already an admin session, row or no row', async () => {
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/bypass')
      .set('Content-Type', 'application/json')
      .send({ email: ' ADMIN@example.com ' });
    expect(res.status).toBe(403);
    expect(res.body.error).toBe('admin_email_not_allowed');
    // Only the trial-cookie lookup ran: the customers table was not consulted.
    expect(mockQuery).toHaveBeenCalledTimes(1);
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it.each([
    ['production', 'production'],
    ['staging', 'staging'],
    ['unset', undefined],
    ['empty', ''],
  ])('is inert when NODE_ENV is %s, even with the flag set', async (_label, nodeEnv) => {
    if (nodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = nodeEnv;
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/bypass')
      .set('Content-Type', 'application/json')
      .send({ email: 'fresh@example.com' });
    expect(res.status).toBe(403);
    expect(res.body.error).toBe('bypass_blocked_in_production');
    expect(mockQuery).toHaveBeenCalledTimes(1); // trial cookie only
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it('does not render the bypass control when NODE_ENV is not development or test', async () => {
    process.env.NODE_ENV = 'staging';
    setupTrialCookieMocks();
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).not.toContain('id="bypass-email"');
  });
});

describe('dev-only admin overrides are inert outside development and test', () => {
  const BUILD = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
  const saved: Record<string, string | undefined> = {};

  beforeEach(() => {
    mockQuery.mockReset();
    for (const key of ['NODE_ENV', 'ADMIN_EMAIL']) saved[key] = process.env[key];
    process.env.ADMIN_EMAIL = 'admin@example.com';
  });

  afterEach(() => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  it.each([['production'], ['staging'], [undefined]])(
    '?dev_trial= is ignored for a real admin session when NODE_ENV is %s',
    async (nodeEnv) => {
      const agent = await loginAsLighthouseAgent('admin@example.com');
      if (nodeEnv === undefined) delete process.env.NODE_ENV;
      else process.env.NODE_ENV = nodeEnv;
      mockQuery.mockReset();
      mockQuery.mockResolvedValue({ rows: [ACTIVE_TRIAL_ROW] });
      const other = '99999999-8888-4777-8666-555555555555';
      await agent.get(`/lighthouse/table/recall?format=json&dev_trial=${other}`);
      // The override would SELECT trial_keepers by trial_id with the UUID
      // from the query string. No query may carry that UUID.
      const usedOverride = mockQuery.mock.calls.some(
        ([, params]) => Array.isArray(params) && params.includes(other)
      );
      expect(usedOverride).toBe(false);
    }
  );

  it.each([['staging'], [undefined]])(
    'the admin lamp read is refused when NODE_ENV is %s',
    async (nodeEnv) => {
      const agent = await loginAsLighthouseAgent('admin@example.com');
      if (nodeEnv === undefined) delete process.env.NODE_ENV;
      else process.env.NODE_ENV = nodeEnv;
      mockQuery.mockReset();
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: BUILD, customer_id: 'cust-other-keeper', app_name: 'Someone else', status: 'preview',
          review_status: 'approved', review_notes: null, preview_url: 'https://x.example', live_url: null,
        }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] }); // the trial does not own it
      const res = await agent.get(`/lighthouse/lamp/${BUILD}`);
      expect(res.status).toBe(404);
    }
  );
});

// ----------------------------------------------------------------------
// POST /lighthouse/build: keeper-initiated v2 spec extraction + dispatch.
// ----------------------------------------------------------------------

describe('GET /lighthouse/table: self-serve build CTA', () => {
  // GET /table query order (non-keeper): trial-cookie, loadTurnHistory,
  // initialMemory, selfServeBuild eligibility (last).
  function setupTableThenEligibility(eligibility: Record<string, unknown>): void {
    setupTrialCookieMocks(); // trial-cookie
    mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
    mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
    mockQuery.mockResolvedValueOnce({ rows: [eligibility] }); // selfServeBuild
  }

  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders the "Build my free preview" CTA when ready (enough turns, real human)', async () => {
    setupTableThenEligibility({
      email_captured: 'maya@example.com',
      handoff_build_id: null,
      is_synthetic: false,
      turn_count: '6',
    });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Build my free preview');
    expect(res.text).toContain('/lighthouse/build');
  });

  it('omits the email field on the ready CTA when an email is already captured', async () => {
    setupTableThenEligibility({
      email_captured: 'maya@example.com',
      handoff_build_id: null,
      is_synthetic: false,
      turn_count: '6',
    });
    const res = await request(app).get('/lighthouse/table');
    expect(res.text).toContain('Build my free preview');
    expect(res.text).not.toContain('id="ss-email"');
  });

  it('shows an inline email field on the ready CTA when no email captured', async () => {
    setupTableThenEligibility({
      email_captured: null,
      handoff_build_id: null,
      is_synthetic: false,
      turn_count: '6',
    });
    const res = await request(app).get('/lighthouse/table');
    expect(res.text).toContain('Build my free preview');
    expect(res.text).toContain('id="ss-email"');
  });

  it('links to the lamp when a build is already underway (building state)', async () => {
    setupTableThenEligibility({
      email_captured: 'maya@example.com',
      handoff_build_id: 'build-xyz',
      is_synthetic: false,
      turn_count: '6',
    });
    const res = await request(app).get('/lighthouse/table');
    expect(res.text).toContain('/lighthouse/lamp/build-xyz');
    expect(res.text).toContain('being built');
    expect(res.text).not.toContain('Build my free preview');
  });

  it('hides the CTA for a trial that has already had its build, even though none is attached any more', async () => {
    setupTableThenEligibility({
      email_captured: 'maya@example.com',
      handoff_build_id: null,
      handoff_at: new Date('2026-05-01T00:00:00Z'),
      is_synthetic: false,
      turn_count: '9',
    });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).not.toContain('Build my free preview');
    expect(res.text).not.toContain('being built');
  });

  it('hides the CTA below the readiness floor (not_ready)', async () => {
    setupTableThenEligibility({
      email_captured: 'maya@example.com',
      handoff_build_id: null,
      is_synthetic: false,
      turn_count: '1',
    });
    const res = await request(app).get('/lighthouse/table');
    expect(res.text).not.toContain('Build my free preview');
  });

  it('hides the CTA for a synthetic (bot) trial even with enough turns', async () => {
    setupTableThenEligibility({
      email_captured: 'bot@example.com',
      handoff_build_id: null,
      is_synthetic: true,
      turn_count: '9',
    });
    const res = await request(app).get('/lighthouse/table');
    expect(res.text).not.toContain('Build my free preview');
  });

  // A logged-in customer's build is owned by their account, and a build owned
  // by an account whose address the build engine refuses is never dispatched.
  // The page must not offer that account a button that would be refused.
  describe('an account whose own email address the build engine would refuse', () => {
    const READY = { email_captured: null, handoff_build_id: null, is_synthetic: false, turn_count: '9' };
    const ACCOUNT_EMAIL = /SELECT email FROM customers WHERE id = \$1/;
    const NOTICE = 'A website cannot be built for this account: the builder does not accept its email address';

    /** GET /table for a logged-in customer who is not a keeper, with a build-ready conversation. */
    async function tableForLoggedInCustomer(accountEmail: string, eligibility: Record<string, unknown> = READY) {
      const agent = await loginAsLighthouseAgent(accountEmail); // session customerId = cust-1
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [] }); // keepers SELECT: not a keeper
      mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
      mockQuery.mockResolvedValueOnce({ rows: [eligibility] }); // selfServeBuild
      mockQuery.mockResolvedValueOnce({ rows: [{ email: accountEmail }] }); // the account's address, if asked
      const before = mockQuery.mock.calls.length;
      const res = await agent.get('/lighthouse/table').set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
      const asked = mockQuery.mock.calls.slice(before).filter(([sql]) => ACCOUNT_EMAIL.test(String(sql)));
      return { res, asked };
    }

    it.each(["o'brien@example.com", 'maya@my_studio.example', `jos${String.fromCharCode(233)}@example.com`])(
      'a logged-in customer (%s) with a build-ready conversation is told why, and is shown no build button',
      async (accountEmail) => {
        const { res, asked } = await tableForLoggedInCustomer(accountEmail);
        expect(res.status).toBe(200);
        expect(res.text).not.toContain('Build my free preview');
        expect(res.text).not.toContain('id="ss-build-btn"');
        expect(res.text).toContain('id="build-blocked"');
        expect(res.text).toContain(NOTICE);
        expect(res.text).toContain('support@craftandship.io');
        expect(asked.map(([, params]) => params)).toEqual([['cust-1']]);
      }
    );

    it('a logged-in customer with an address the engine accepts is offered the build as before', async () => {
      const { res, asked } = await tableForLoggedInCustomer('maya@example.com');
      expect(res.text).toContain('Build my free preview');
      expect(res.text).not.toContain('id="build-blocked"');
      expect(asked).toHaveLength(1);
    });

    it('the account is not read when no build would be offered anyway, or when nobody is logged in', async () => {
      const notReady = await tableForLoggedInCustomer("o'brien@example.com", { ...READY, turn_count: '1' });
      expect(notReady.res.text).not.toContain('Build my free preview');
      expect(notReady.res.text).not.toContain('id="build-blocked"');
      expect(notReady.asked).toEqual([]);

      mockQuery.mockReset();
      setupTableThenEligibility(READY);
      const anonymous = await request(app).get('/lighthouse/table');
      // A visitor's address is only something typed: the build step answers for it.
      expect(anonymous.text).toContain('Build my free preview');
      expect(mockQuery.mock.calls.some(([sql]) => ACCOUNT_EMAIL.test(String(sql)))).toBe(false);
    });

    it('a keeper with no build yet: the same notice in place of "Build my website"', async () => {
      const forKeeper = async (accountEmail: string) => {
        mockQuery.mockReset();
        const agent = await loginAsLighthouseAgent(accountEmail);
        mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
        mockQuery.mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] }); // keepers SELECT: a keeper, no build
        mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
        mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
        mockQuery.mockResolvedValueOnce({ rows: [{ email: accountEmail }] }); // the account's address
        return agent.get('/lighthouse/table').set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
      };
      const refused = await forKeeper("o'brien@example.com");
      expect(refused.status).toBe(200);
      expect(refused.text).not.toContain('Build my website');
      expect(refused.text).not.toContain('id="build-btn"');
      expect(refused.text).toContain(NOTICE);

      const accepted = await forKeeper('keeper@example.com');
      expect(accepted.text).toContain('Build my website');
      expect(accepted.text).not.toContain('id="build-blocked"');
    });
  });
});

describe('POST /lighthouse/build: self-serve trial path', () => {
  // guard SELECT shape: is_synthetic, email_captured, handoff_build_id
  function guard(over: Record<string, unknown> = {}): { rows: unknown[] } {
    return {
      rows: [{ is_synthetic: false, email_captured: 'maya@example.com', handoff_build_id: null, ...over }],
    };
  }

  beforeEach(() => {
    mockQuery.mockReset();
    mockExtract.mockReset();
    mockDispatch.mockReset();
  });

  it('an address the dispatch will not use: 409 in words that do not say an account exists, asks for another, forgets the refused one', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard({ email_captured: 'victim@example.com' })); // guard SELECT
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
    mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 }); // UPDATE ... email_captured = NULL
    const dispatchModule = await import('../../src/services/harbor_dispatch');
    mockDispatch.mockRejectedValueOnce(
      new dispatchModule.HarborDispatchError('account_exists', 'The address belongs to a verified account.')
    );
    const res = await request(app).post('/lighthouse/build').send({});
    expect(res.status).toBe(409);
    expect(res.body).toMatchObject({ error: 'email_not_usable', requiresEmail: true });
    // Neither the service's code nor its sentence reaches the visitor.
    expect(JSON.stringify(res.body)).not.toMatch(/account_exists|already exists|verified/i);
    const cleared = mockQuery.mock.calls.find(([sql]) => /SET email_captured = NULL/.test(String(sql)));
    expect(cleared).toBeDefined();
    expect(String(cleared?.[0])).toMatch(/handoff_build_id IS NULL/);
    expect(cleared?.[1]).toEqual([TRIAL_ID]);
  });

  it('a typed address the build engine would refuse gets the same answer, word for word, as one that has an account', async () => {
    const dispatchModule = await import('../../src/services/harbor_dispatch');
    const refuseWith = async (code: 'account_exists' | 'email_not_buildable', captured: string) => {
      mockQuery.mockReset();
      setupTrialCookieMocks();
      mockQuery.mockResolvedValueOnce(guard({ email_captured: captured })); // guard SELECT
      mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 }); // UPDATE ... email_captured = NULL
      mockDispatch.mockRejectedValueOnce(new dispatchModule.HarborDispatchError(code, 'for the log only'));
      const res = await request(app).post('/lighthouse/build').send({});
      const cleared = mockQuery.mock.calls.filter(([sql]) => /SET email_captured = NULL/.test(String(sql)));
      return { status: res.status, body: res.body, cleared: cleared.map(([, params]) => params) };
    };
    const hasAccount = await refuseWith('account_exists', 'victim@example.com');
    const notBuildable = await refuseWith('email_not_buildable', "o'brien@example.com");
    expect(notBuildable).toEqual(hasAccount);
    expect(notBuildable.status).toBe(409);
    expect(notBuildable.body).toEqual({
      error: 'email_not_usable',
      requiresEmail: true,
      message: 'I could not use that email address for your preview. Please give me a different one, or log in first if you have an account here.',
    });
    // The refused address is forgotten, so the visitor can give another.
    expect(notBuildable.cleared).toEqual([[TRIAL_ID]]);
    // The owner's sentence is for an account's own address; a visitor never sees it.
    expect(JSON.stringify(notBuildable.body)).not.toContain('builder does not accept');
  });

  it('a logged-in customer whose own account address the builder cannot use is told so plainly, and is not asked for another address', async () => {
    const dispatchModule = await import('../../src/services/harbor_dispatch');
    const agent = await loginAsLighthouseAgent("o'brien@example.com"); // session customerId = cust-1
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
    mockQuery.mockResolvedValueOnce({ rows: [] }); // keepers SELECT: not a keeper
    mockQuery.mockResolvedValueOnce(guard({ email_captured: null })); // trial guard
    mockQuery.mockResolvedValueOnce({ rows: [{ email: "o'brien@example.com" }] }); // the session account's address
    mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 }); // the address recorded on the trial
    mockQuery.mockResolvedValueOnce({ rows: [{ trial_id: TRIAL_ID }] }); // the trial's cookie replaced (rotateTrialCookie)
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
    mockDispatch.mockRejectedValueOnce(
      new dispatchModule.HarborDispatchError('email_not_buildable', dispatchModule.OWNER_EMAIL_NOT_BUILDABLE_MESSAGE)
    );
    const res = await agent.post('/lighthouse/build').set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`).send({ email: 'someone-else@example.com' });
    expect(res.status).toBe(409);
    expect(res.body).toEqual({ error: 'email_not_buildable', message: dispatchModule.OWNER_EMAIL_NOT_BUILDABLE_MESSAGE });
    expect(res.body.message).toMatch(/A website cannot be built for this account: the builder does not accept its email address/);
    // The dispatch was asked about the account's address, not the typed one.
    expect(mockDispatch.mock.calls[0][1]).toMatchObject({ resolvedEmail: "o'brien@example.com", anonymous: false });
    // Nothing is forgotten: the address is the account's, not something typed.
    expect(mockQuery.mock.calls.some(([sql]) => /SET email_captured = NULL/.test(String(sql)))).toBe(false);
  });

  it('a cookie-only visitor dispatches as anonymous, and a typed address may not be a verified account', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard()); // guard SELECT
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
    mockDispatch.mockResolvedValueOnce({ buildId: 'b-anon', alreadyDispatched: false, warnings: [] });
    const res = await request(app).post('/lighthouse/build').send({});
    expect(res.status).toBe(200);
    expect(mockDispatch.mock.calls[0][1]).toMatchObject({
      trialId: TRIAL_ID,
      resolvedEmail: 'maya@example.com',
      source: 'self_serve',
      allowVerifiedAccount: false,
      anonymous: true,
    });
  });

  it('a customer at a build-start limit is answered 429 with the sentence that says why', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard()); // guard SELECT
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
    const dispatchModule = await import('../../src/services/harbor_dispatch');
    mockDispatch.mockRejectedValueOnce(
      new dispatchModule.HarborDispatchError('build_limit', 'You already have builds in progress. Please wait for one to finish before starting another.')
    );
    const res = await request(app).post('/lighthouse/build').send({});
    expect(res.status).toBe(429);
    expect(res.body).toMatchObject({ error: 'build_limit' });
    expect(res.body.message).toMatch(/builds in progress/);
  });

  it('answers 503 (not 500) when the shared daily model budget is spent', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard()); // guard SELECT
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
    const { LlmDailyBudgetError } = await import('../../src/services/llm-budget');
    mockDispatch.mockRejectedValueOnce(new LlmDailyBudgetError());
    const res = await request(app).post('/lighthouse/build').send({});
    expect(res.status).toBe(503);
    expect(res.body.error).toBe('busy');
    expect(res.body.message).toMatch(/try again later/i);
  });

  it('answers 503 with the plain explanation (not 500) when the server has no model key', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard()); // guard SELECT
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
    const { AssistantNotConfiguredError } = await import('../../src/utils/assistant');
    mockDispatch.mockRejectedValueOnce(new AssistantNotConfiguredError());
    const res = await request(app).post('/lighthouse/build').send({});
    expect(res.status).toBe(503);
    expect(res.body.code).toBe('assistant_not_configured');
    expect(res.body.message).toMatch(/not configured on this server/);
  });

  it('blocks a bot (is_synthetic) trial with 403, never dispatches', async () => {
    setupTrialCookieMocks(); // trial-cookie middleware load
    mockQuery.mockResolvedValueOnce(guard({ is_synthetic: true })); // guard SELECT
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(403);
    expect(res.body.error).toBe('bot_blocked');
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it('asks for an email (402 requiresEmail) when none captured + none provided', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard({ email_captured: null }));
    const res = await request(app).post('/lighthouse/build').send({});
    expect(res.status).toBe(402);
    expect(res.body.requiresEmail).toBe(true);
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it('rejects an invalid email shape (400 requiresEmail)', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard({ email_captured: null }));
    const res = await request(app).post('/lighthouse/build').send({ email: 'not-an-email' });
    expect(res.status).toBe(400);
    expect(res.body.requiresEmail).toBe(true);
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it('holds at the readiness floor (409 not_ready) below min turns', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard()); // guard (email present)
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '2' }] }); // turn count (< 4)
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('not_ready');
    expect(res.body.minTurns).toBe(4);
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it('is idempotent: returns the existing build when one is attached', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard({ handoff_build_id: 'existing-build-id' }));
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(200);
    expect(res.body.alreadyDispatched).toBe(true);
    expect(res.body.buildId).toBe('existing-build-id');
    expect(res.body.redirect).toBe('/lighthouse/lamp/existing-build-id');
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it('one free preview per trial: a trial whose build was dispatched and later deleted is refused, with no extraction', async () => {
    // The stamp (handoff_build_id) is ON DELETE SET NULL and the worker prunes
    // old failed builds, so "no build attached" is not "never dispatched".
    // handoff_at is what the route reads.
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard({ handoff_build_id: null, handoff_at: new Date('2026-05-01T00:00:00Z') }));
    const res = await request(app).post('/lighthouse/build').send({ email: 'again@example.com' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('build_already_used');
    expect(mockDispatch).not.toHaveBeenCalled();
    // Refused before the email was stored and before the turn count was read.
    expect(mockQuery).toHaveBeenCalledTimes(2);
    expect(String(mockQuery.mock.calls[1][0])).toMatch(/SELECT is_synthetic, email_captured, handoff_build_id, handoff_at/);
  });

  it('build_already_used from the dispatch service (the check under the row lock) is a 409 too', async () => {
    const { HarborDispatchError } = await import('../../src/services/harbor_dispatch');
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard());
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '6' }] });
    mockDispatch.mockRejectedValueOnce(new HarborDispatchError('build_already_used', 'Already built.'));
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('build_already_used');
  });

  it('happy path: dispatches a free build + returns redirect to the lamp', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard()); // guard (email present)
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '6' }] }); // turn count (>= 4)
    mockDispatch.mockResolvedValueOnce({
      buildId: 'fresh-build-id',
      alreadyDispatched: false,
      warnings: [],
      customerId: 'cust-1',
      tier: 'build',
    });
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(200);
    expect(res.body.buildId).toBe('fresh-build-id');
    expect(res.body.redirect).toBe('/lighthouse/lamp/fresh-build-id');
    expect(mockDispatch).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ source: 'self_serve', refuseBarrenSpec: true }),
    );
  });

  it('refuses a barren spec from the service (409 barren_spec)', async () => {
    const { HarborDispatchError } = await import('../../src/services/harbor_dispatch');
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard());
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '6' }] });
    mockDispatch.mockRejectedValueOnce(
      new HarborDispatchError('barren_spec', 'Not enough yet.')
    );
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('barren_spec');
  });

  // The ways the extraction itself fails. Each has a status of its own and a
  // sentence; none is a 500, and none is the HTML error page.
  it.each([
    ['model_call_failed', 502],
    ['unusable_reply', 502],
  ] as const)('an extraction that ends in %s is answered %i in JSON, with the error\'s sentence', async (code, status) => {
    const { SpecExtractionError } = await import('../../src/services/spec_extraction');
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard());
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '6' }] });
    mockDispatch.mockRejectedValueOnce(new SpecExtractionError(code, 'Nothing was built.'));
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(status);
    expect(res.headers['content-type']).toMatch(/^application\/json/);
    expect(res.body).toEqual({ error: code, message: 'Nothing was built.' });
  });

  it('nothing the extraction may read is a 409 that asks for more conversation, in the readiness floor\'s words', async () => {
    const { SpecExtractionError } = await import('../../src/services/spec_extraction');
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard());
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '6' }] });
    mockDispatch.mockRejectedValueOnce(new SpecExtractionError('nothing_to_extract', 'There is nothing in this conversation yet that a website can be built from.'));
    const res = await request(app).post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('nothing_to_extract');
    // The visitor is asked to say more, not told that what they wrote was set aside.
    expect(res.body.message).toMatch(/talk a little more first/);
  });

  it('an error nobody mapped is a 500 in JSON for this route, with or without a JSON body', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      for (const send of [
        (r: request.Test) => r.send({}),
        // The keeper's build button posts no body and no Content-Type.
        (r: request.Test) => r,
      ]) {
        mockQuery.mockReset();
        setupTrialCookieMocks();
        mockQuery.mockResolvedValueOnce(guard());
        mockQuery.mockResolvedValueOnce({ rows: [{ c: '6' }] });
        mockDispatch.mockRejectedValueOnce(new Error('a database error'));
        const res = await send(request(app).post('/lighthouse/build'));
        expect(res.status).toBe(500);
        expect(res.headers['content-type']).toMatch(/^application\/json/);
        expect(res.body.code).toBe('server_error');
        expect(res.text).not.toMatch(/<!DOCTYPE|<html/i);
      }
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('the readiness floor and the page count the turns the extraction may read, with one statement each', async () => {
    const { extractableTurnCountSql } = await import('../../src/services/spec_extraction');
    const FILTER = /NOT \('crisis' = ANY\(ct\.rail_flags\)\)[\s\S]*km\.source_turn_id = ct\.turn_id AND km\.partition = 'sacred'/;
    // The fragment itself carries the extraction's filter.
    expect(extractableTurnCountSql('trial_id', '$1')).toMatch(FILTER);

    // POST /lighthouse/build: the floor's count.
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce(guard());
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '2' }] });
    await request(app).post('/lighthouse/build');
    const floorSql = mockQuery.mock.calls.map(([sql]) => String(sql)).filter((sql) => /COUNT\(\*\) FROM companion_turns/.test(sql));
    expect(floorSql).toHaveLength(1);
    expect(floorSql[0]).toContain(extractableTurnCountSql('trial_id', '$1'));

    // GET /lighthouse/table: the count behind the build button.
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    setupTrialCookieMocks();
    await request(app).get('/lighthouse/table');
    const pageSql = mockQuery.mock.calls.map(([sql]) => String(sql)).filter((sql) => /COUNT\(\*\) FROM companion_turns/.test(sql));
    expect(pageSql).toHaveLength(1);
    expect(pageSql[0]).toContain(extractableTurnCountSql('trial_id', 't.trial_id'));
  });
});

describe('GET /lighthouse/table/recall', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders HTML by default with the keeper memory items', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          memory_id: 'm1',
          partition: 'working',
          category: 'audience',
          content: 'River produces conscious dance events.',
          keeper_phrase: 'I produce conscious dance',
          created_at: new Date(),
          updated_at: new Date(),
        },
      ],
    });
    const res = await request(app).get('/lighthouse/table/recall');
    expect(res.status).toBe(200);
    expect(res.text).toContain('River produces conscious dance events');
    expect(res.text).toMatch(/What Haven remembers/i);
  });

  it('returns JSON when format=json', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({
      rows: [
        { memory_id: 'm1', partition: 'working', category: 'fact', content: 'X', keeper_phrase: null, created_at: new Date(), updated_at: new Date() },
      ],
    });
    const res = await request(app).get('/lighthouse/table/recall?format=json');
    expect(res.status).toBe(200);
    expect(res.body.items).toHaveLength(1);
    expect(res.body.items[0].content).toBe('X');
  });

  it('renders an empty-state message when there is no memory yet', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table/recall');
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Nothing here yet/);
  });

  it('shows lite-mode note when OPENAI_API_KEY is unset', async () => {
    const ORIGINAL_OPENAI = process.env.OPENAI_API_KEY;
    delete process.env.OPENAI_API_KEY;
    try {
      setupTrialCookieMocks();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await request(app).get('/lighthouse/table/recall');
      expect(res.status).toBe(200);
      expect(res.text).toMatch(/lite mode/i);
    } finally {
      if (ORIGINAL_OPENAI === undefined) delete process.env.OPENAI_API_KEY;
      else process.env.OPENAI_API_KEY = ORIGINAL_OPENAI;
    }
  });

  it('SQL filters trial-id + non-deleted, limited to 200 rows', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    await request(app).get('/lighthouse/table/recall');
    const recallCall = mockQuery.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('FROM keeper_memory')
    );
    expect(recallCall).toBeDefined();
    expect(recallCall![0]).toMatch(/trial_id = \$1/);
    expect(recallCall![0]).toMatch(/deleted_at IS NULL/);
    expect(recallCall![0]).toMatch(/LIMIT \$2/);
    expect(recallCall![1]).toEqual([TRIAL_ID, 200]);
  });

  it('a keeper with an account session is asked for by keeper id, not by the cookie\'s trial', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com'); // session customerId = cust-1
    mockQuery.mockReset();
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] }); // keepers row for the session
    mockQuery.mockResolvedValueOnce({
      rows: [{ memory_id: 'm1', partition: 'working', category: 'fact', content: 'The keeper teaches reiki.', keeper_phrase: null, created_at: new Date(), updated_at: new Date() }],
    });
    const res = await agent.get('/lighthouse/table/recall?format=json').set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.body.items).toHaveLength(1);
    const recallCall = mockQuery.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('FROM keeper_memory')
    );
    expect(recallCall![0]).toMatch(/keeper_id = \$1/);
    expect(recallCall![0]).not.toMatch(/trial_id = \$1/);
    expect(recallCall![1]).toEqual(['cust-1', 200]);
  });
});

// ===== /lighthouse/lamp/:buildId (make-it-public flow, 2026-04-28) =====

const BUILD_ID = '99999999-aaaa-bbbb-cccc-dddddddddddd';

function buildRow(overrides: Record<string, unknown> = {}) {
  return {
    id: BUILD_ID,
    customer_id: null,
    app_name: 'Maya Reiki',
    status: 'preview',
    review_status: 'approved',
    review_notes: null,
    preview_url: 'https://maya-reiki-example.up.railway.app',
    live_url: null,
    ...overrides,
  };
}

/**
 * Setup the SQL response chain that resolveLampOwnership produces:
 *   1. trial-cookie middleware (active trial row)
 *   2. SELECT builds WHERE id = $1
 *   3. SELECT trial_keepers WHERE trial_id = $1 AND handoff_build_id = $2  (only if trial path)
 */
function setupTrialOwnedBuild(build = buildRow()): void {
  setupTrialCookieMocks();                                // (1)
  mockQuery.mockResolvedValueOnce({ rows: [build] });     // (2)
  mockQuery.mockResolvedValueOnce({ rows: [{ trial_id: TRIAL_ID }] }); // (3)
}

describe('GET /lighthouse/lamp/:buildId', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders the preview-approved state with Make-it-public CTA', async () => {
    setupTrialOwnedBuild();
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Make it public/);
    expect(res.text).toContain('https://maya-reiki-example.up.railway.app');
  });

  it('does not call the preview private (it is a public address with no access control)', async () => {
    setupTrialOwnedBuild();
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toContain('Your preview is ready');
    expect(res.text).not.toMatch(/private/i);
    expect(res.text).not.toMatch(/no one finds it|only you and people you share/i);
    expect(res.text).toContain('Anyone who has this address can open the preview.');
  });

  it('renders queued/building state without preview URL', async () => {
    setupTrialOwnedBuild(buildRow({ status: 'building', review_status: null, preview_url: null }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Shaping the lamp/);
    expect(res.text).not.toMatch(/Make it public/);
    // The preview address is emailed when the build arrives at preview; no
    // review comes before it, so the page must not promise one.
    expect(res.text).toContain('we will email you its preview address');
    expect(res.text).not.toMatch(/Senior Web Craftsman|before you ever see a preview/);
  });

  it('renders preview+pending review (no Make-it-public CTA yet)', async () => {
    setupTrialOwnedBuild(buildRow({ review_status: 'pending' }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Your draft is in review/);
    expect(res.text).toContain('Its preview address was sent to your email when the build finished.');
    expect(res.text).not.toMatch(/business day/);
    expect(res.text).toContain('needs a recorded review approval before it goes live');
    // Nobody is said to be "reading every page right now": nothing records that.
    expect(res.text).not.toMatch(/Senior Web Craftsman|reading every page/);
    expect(res.text).not.toMatch(/Make it public/);
  });

  it('renders changes_requested with notes (blocks Make-it-public)', async () => {
    setupTrialOwnedBuild(buildRow({ review_status: 'changes_requested', review_notes: 'Tighten home page intro.' }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/few notes/i);
    expect(res.text).toContain('Tighten home page intro.');
    expect(res.text).not.toMatch(/Make it public/);
  });

  it('renders live state with public URL', async () => {
    setupTrialOwnedBuild(buildRow({ status: 'live', live_url: 'https://maya-reiki.example.com' }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    // 2026-04-28: live-state eyebrow reframed from "The lamp is set" (metaphor)
    // to plain "Your website is public" per founder direction.
    expect(res.text).toMatch(/website is public/i);
    expect(res.text).toContain('https://maya-reiki.example.com');
  });

  it('renders failed state pointing back to the conversation', async () => {
    setupTrialOwnedBuild(buildRow({ status: 'failed' }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Something went wrong/);
    expect(res.text).toMatch(/href="\/lighthouse\/table">your conversation<\/a>/);
    // "keeper" is internal vocabulary; the customer never sees it.
    expect(res.text).not.toMatch(/keeper/i);
  });

  it('returns 404 for non-existent build', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });          // build SELECT empty
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(404);
  });

  it('returns 404 when trial does NOT own the build', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [buildRow()] });   // build exists
    mockQuery.mockResolvedValueOnce({ rows: [] });             // but no trial_keepers row links it
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(404);
  });

  it('returns 404 for malformed UUID', async () => {
    setupTrialCookieMocks();
    const res = await request(app).get('/lighthouse/lamp/not-a-uuid').set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(404);
  });

  it('lamp view contains no em dashes (brand voice)', async () => {
    setupTrialOwnedBuild();
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.text).not.toMatch(/\u2014/);
    expect(res.text).not.toMatch(/&mdash;/i);
  });
});

describe('GET /lighthouse/preview', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders the picker with all 5 families when no archetype is known', async () => {
    setupTrialCookieMocks();
    // No design_pref rows -> archetype stays null, mood meta stays empty
    mockQuery.mockResolvedValueOnce({ rows: [] }); // archetype lookup
    mockQuery.mockResolvedValueOnce({ rows: [] }); // pref lookup
    const res = await request(app).get('/lighthouse/preview');
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/How would you like your site to look/);
    // All 5 family names should appear when no archetype filter
    expect(res.text).toContain('Classic Business');
    expect(res.text).toContain('Editorial Statement');
    expect(res.text).toContain('Directory Rich');
    expect(res.text).toContain('Simple Statement');
    expect(res.text).toContain('Event Led');
  });

  it('filters to compatible families when archetype query param is set', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/preview?archetype=fitness-studio');
    expect(res.status).toBe(200);
    // fitness-studio compatible: classic-business, event-led only
    expect(res.text).toContain('Classic Business');
    expect(res.text).toContain('Event Led');
    expect(res.text).not.toContain('Editorial Statement');
    expect(res.text).not.toContain('Directory Rich');
    expect(res.text).not.toContain('Simple Statement');
  });

  it('renders a "Recommended" badge on position 0 when scoring is non-zero', async () => {
    setupTrialCookieMocks();
    // With ?archetype query param, the archetype DB lookup is skipped;
    // only the pref lookup runs. Pref returns mood_keywords -> editorial
    // scores high -> Recommended badge.
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          content: JSON.stringify({
            mood_keywords: ['airy', 'editorial', 'quiet', 'minimal'],
          }),
        },
      ],
    });
    const res = await request(app).get('/lighthouse/preview?archetype=healer-wellness');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Recommended');
  });

  it('400-renders an error when no trial context', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] }); // trial cookie middleware finds no row
    const res = await request(app).get('/lighthouse/preview');
    expect(res.status).toBeGreaterThanOrEqual(400);
  });

  it('rejects unknown archetype query param defensively (silent fallback to "all")', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // An archetype like "Bad Value!" fails the regex; the route falls
    // through to the keeper_memory lookup (which is empty) and renders
    // all families.
    const res = await request(app).get('/lighthouse/preview?archetype=Bad%20Value!');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Classic Business');
  });

  it('renders the CSS-only mock when no thumbnails on disk', async () => {
    // No thumbnails dir -> EJS falls through to .preview-mock block.
    const prev = process.env.CAS_TEMPLATE_THUMBNAILS_DIR;
    process.env.CAS_TEMPLATE_THUMBNAILS_DIR = '/tmp/cas-thumbnails-does-not-exist-' + Date.now();
    try {
      setupTrialCookieMocks();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await request(app).get('/lighthouse/preview');
      expect(res.status).toBe(200);
      expect(res.text).toContain('class="preview-mock"');
      expect(res.text).not.toMatch(/\/template-thumbnails\/[a-z-]+\/home\.png/);
    } finally {
      if (prev === undefined) delete process.env.CAS_TEMPLATE_THUMBNAILS_DIR;
      else process.env.CAS_TEMPLATE_THUMBNAILS_DIR = prev;
    }
  });

  it('stamps descriptive alt text on rendered thumbnails', async () => {
    // Descriptive alt for screen-reader users. Each <img> alt must
    // carry the family's thumbnail_alt_summary plus the structural
    // decision aid sighted users see in the specs list: h1_rem and
    // nav style. Verified against the real thumbnail-on-disk path
    // because the mock-only path doesn't emit <img> tags.
    const fs = await import('fs');
    const os = await import('os');
    const path = await import('path');
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cas-thumbnails-alt-'));
    try {
      const fid = 'editorial-statement';
      fs.mkdirSync(path.join(tmp, fid), { recursive: true });
      const onePxPng = Buffer.from(
        '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082',
        'hex',
      );
      fs.writeFileSync(path.join(tmp, fid, 'home.png'), onePxPng);
      fs.writeFileSync(path.join(tmp, fid, 'about.png'), onePxPng);
      fs.writeFileSync(path.join(tmp, fid, 'services.png'), onePxPng);

      const prev = process.env.CAS_TEMPLATE_THUMBNAILS_DIR;
      process.env.CAS_TEMPLATE_THUMBNAILS_DIR = tmp;
      try {
        setupTrialCookieMocks();
        mockQuery.mockResolvedValueOnce({ rows: [] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await request(app).get('/lighthouse/preview?archetype=healer-wellness');
        expect(res.status).toBe(200);
        // Alt includes the page route name (home/about/services).
        expect(res.text).toMatch(/alt="Editorial Statement home page preview\./);
        expect(res.text).toMatch(/alt="Editorial Statement about page preview\./);
        expect(res.text).toMatch(/alt="Editorial Statement services page preview\./);
        // Alt includes the family's thumbnail_alt_summary phrase.
        expect(res.text).toContain('Magazine-feeling layout with a thin minimal nav');
        // Alt includes h1_rem (editorial-statement: 5rem).
        expect(res.text).toContain('5rem display headlines');
        // Alt includes the nav description.
        expect(res.text).toContain('Nav: Thin minimal bar, wordmark only');
      } finally {
        if (prev === undefined) delete process.env.CAS_TEMPLATE_THUMBNAILS_DIR;
        else process.env.CAS_TEMPLATE_THUMBNAILS_DIR = prev;
      }
    } finally {
      try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });

  it('renders real <img> thumbnails when home.png exists on disk', async () => {
    const fs = await import('fs');
    const os = await import('os');
    const path = await import('path');
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cas-thumbnails-'));
    try {
      // Seed a fake home.png + about.png for classic-business; leave
      // services.png missing to confirm partial coverage works.
      const fid = 'classic-business';
      fs.mkdirSync(path.join(tmp, fid), { recursive: true });
      // 1x1 transparent PNG bytes; the route only checks existence,
      // not validity.
      const onePxPng = Buffer.from(
        '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082',
        'hex',
      );
      fs.writeFileSync(path.join(tmp, fid, 'home.png'), onePxPng);
      fs.writeFileSync(path.join(tmp, fid, 'about.png'), onePxPng);

      const prev = process.env.CAS_TEMPLATE_THUMBNAILS_DIR;
      process.env.CAS_TEMPLATE_THUMBNAILS_DIR = tmp;
      try {
        setupTrialCookieMocks();
        mockQuery.mockResolvedValueOnce({ rows: [] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await request(app).get('/lighthouse/preview');
        expect(res.status).toBe(200);
        // classic-business gets the real thumbnails
        expect(res.text).toContain('/template-thumbnails/classic-business/home.png');
        expect(res.text).toContain('/template-thumbnails/classic-business/about.png');
        // services.png missing -> no img for it
        expect(res.text).not.toContain('/template-thumbnails/classic-business/services.png');
        // other families still fall back to the CSS mock
        expect(res.text).toContain('class="preview-mock"');
      } finally {
        if (prev === undefined) delete process.env.CAS_TEMPLATE_THUMBNAILS_DIR;
        else process.env.CAS_TEMPLATE_THUMBNAILS_DIR = prev;
      }
    } finally {
      try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });
});

describe('GET /lighthouse/lamp/:buildId/make-public', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders the form with prefilled email + CSRF token', async () => {
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: 'maya@example.com' }] }); // resolvePrefilledEmail trial path
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/make-public`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Make this public/);
    expect(res.text).toContain('maya@example.com');
    expect(res.text).toContain('name="_csrf"');
    expect(res.text).toContain('name="monthly_amount_cents"');
    expect(res.text).toContain('name="email"');
  });

  it('redirects to /lamp when build is not preview+approved', async () => {
    setupTrialOwnedBuild(buildRow({ status: 'building', review_status: null }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/make-public`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe(`/lighthouse/lamp/${BUILD_ID}`);
  });

  it('renders changes-first page (409) when review_status=changes_requested', async () => {
    setupTrialOwnedBuild(buildRow({ review_status: 'changes_requested', review_notes: 'Fix CTAs.' }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/make-public`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(409);
    expect(res.text).toMatch(/few notes first/i);
    expect(res.text).toContain('Fix CTAs.');
  });

  it('returns 404 for malformed UUID without rendering the form', async () => {
    setupTrialCookieMocks();
    const res = await request(app).get('/lighthouse/lamp/abc/make-public').set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(404);
  });

  it('form contains the slider min/max + attestation block', async () => {
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: '' }] });
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/make-public`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.text).toContain('min="1900"');
    expect(res.text).toContain('max="3900"');
    // 2026-04-28: attestation copy reframed from "Your word is enough"
    // to "This amount fits my practice." per founder direction (drop
    // sliding-scale jargon entirely).
    expect(res.text).toMatch(/fits my practice/i);
    expect(res.text).toContain('whatever fits your practice');
  });

  // ─────────────────────────────────────────────────────────────────
  // Legal disclosure (harbor-make-public-legal-links-near-cta, 2026-05-09)
  //
  // Keeper paying $19-$39/month should see a link to /terms + /privacy
  // before paying. /lighthouse/lamp/:buildId/make-public is a standalone
  // doc that does NOT inherit the layout-harbor footer where those
  // links normally live, so we surface them inline above the pay CTA.
  // ─────────────────────────────────────────────────────────────────

  it('renders /terms + /privacy links above the pay CTA, opening in new tab', async () => {
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: 'maya@example.com' }] });
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/make-public`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    // Both links present.
    expect(res.text).toMatch(
      /<a[^>]*href="\/terms"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/
    );
    expect(res.text).toMatch(
      /<a[^>]*href="\/privacy"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/
    );
    // Text reads as a disclosure ("By paying you agree to..."), not a
    // legal-disclaimer wall.
    expect(res.text).toMatch(/By paying you agree to our/i);
    // Disclosure must precede the submit button in document order.
    const legalIdx = res.text.indexOf('legal-line');
    const submitIdx = res.text.indexOf('type="submit"');
    expect(legalIdx).toBeGreaterThan(0);
    expect(submitIdx).toBeGreaterThan(legalIdx);
  });

  it('attestation checkbox label has accessible association via for=att', async () => {
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: '' }] });
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/make-public`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    // Attestation input id="att" must have a matching <label for="att">
    // (a11y audit cross-check; static-analysis pass on 2026-05-09 already
    // verified, but a regression guard prevents drift).
    expect(res.text).toMatch(/<input[^>]*type="checkbox"[^>]*id="att"/);
    expect(res.text).toMatch(/<label\s+for="att"/);
  });
});

describe('POST /lighthouse/lamp/:buildId/make-public', () => {
  // The form's token is good only in the session that rendered the form, so
  // the POST carries that session's cookie as a browser would ("; cas.sid=...",
  // set by csrfToken(); empty when a test fetched no form).
  let formSession = '';
  beforeEach(() => {
    mockQuery.mockReset();
    formSession = '';
  });

  async function csrfToken(): Promise<string> {
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: '' }] });
    const formRes = await request(app)
      .get(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    const sessionCookie = ([] as string[]).concat(formRes.headers['set-cookie'] || []).find((c) => c.startsWith('cas.sid='));
    formSession = sessionCookie ? `; ${sessionCookie.split(';')[0]}` : '';
    const m = formRes.text.match(/name="_csrf"\s+value="([^"]+)"/);
    return m ? m[1] : '';
  }

  it('rejects POST without a valid CSRF token (403)', async () => {
    setupTrialOwnedBuild();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ monthly_amount_cents: '3900', email: 'maya@example.com' });
    expect(res.status).toBe(403);
  });

  it('rejects amount below floor (400 + invalid_amount)', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '100', email: 'maya@example.com', sliding_scale_attestation: 'on' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('invalid_amount');
  });

  it('rejects below-default amount without attestation (400)', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '2900', email: 'maya@example.com' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('attestation_required_for_sliding_scale');
  });

  it('rejects missing/invalid email (400)', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: '' }] }); // resolvePrefilledEmail trial path
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: '' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('valid_email_required');
  });

  it('happy path: redirects 303 to Stripe Checkout URL', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // no account has this email yet
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'maya@example.com', email_confirmed: 'on' });
    expect(res.status).toBe(303);
    expect(res.headers.location).toBe('https://checkout.stripe.com/mock-sub-session');
  });

  it('a trial must confirm the address before Checkout: without the confirmation, 400 and no Checkout session', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // no account has this email yet
    const stripeMod = await import('../../src/services/stripe');
    const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    create.mockClear();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'maya@example.com' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('email_confirmation_required');
    expect(create).not.toHaveBeenCalled();
  });

  it('returns 409 if build is not preview+approved', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild(buildRow({ status: 'building', review_status: null }));
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'maya@example.com' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('build_not_ready_to_publish');
  });

  it('refuses a trial that names an email belonging to an existing account (no Checkout session)', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-victim' }] }); // the email has an account
    const stripeMod = await import('../../src/services/stripe');
    const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    create.mockClear();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'Victim@Example.com' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('account_exists_login_required');
    // Nothing was created at Stripe: the webhook can never see this pairing.
    expect(create).not.toHaveBeenCalled();
    // The lookup used the normalized address.
    const lookup = mockQuery.mock.calls.find(([sql]) => /FROM customers WHERE email = \$1/.test(String(sql)));
    expect(lookup?.[1]).toEqual(['victim@example.com']);
  });

  it('lets a trial use the email of the account the visitor is logged in as', async () => {
    const agent = await loginAsLighthouseAgent('maya@example.com'); // session customerId = cust-1
    mockQuery.mockReset();
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: '' }] });
    const formRes = await agent
      .get(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    const csrf = (formRes.text.match(/name="_csrf"\s+value="([^"]+)"/) || [])[1] || '';
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] }); // the email is the session's own account
    const res = await agent
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'maya@example.com', email_confirmed: 'on' });
    expect(res.status).toBe(303);
  });

  it('a self-serve build (already attached to its own unsubscribed account) goes on to Checkout with that account\'s address', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild(buildRow({ customer_id: 'cust-preview-owner' }));
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'Maya@Example.com', subscribed: false }] }); // the build's account
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: 'maya@example.com' }] }); // the address this trial gave
    const stripeMod = await import('../../src/services/stripe');
    const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    create.mockClear();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'maya@example.com', email_confirmed: 'on' });
    expect(res.status).toBe(303);
    // The question asked was about the build's customer, not about the typed email.
    const asked = mockQuery.mock.calls.find(([sql]) => /FROM customers c\s+WHERE c\.id = \$1/.test(String(sql)));
    expect(asked?.[1]).toEqual(['cust-preview-owner']);
    expect(String(asked?.[0])).toMatch(/FROM keepers k/);
    expect(mockQuery.mock.calls.some(([sql]) => /FROM customers WHERE email/.test(String(sql)))).toBe(false);
    // Checkout carries the account's address: receipt, mail and account agree.
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ customerEmail: 'maya@example.com', trialId: TRIAL_ID }));
  });

  it('a different address than the account\'s is refused, not silently replaced (no Checkout session)', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild(buildRow({ customer_id: 'cust-preview-owner' }));
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'jane@gmial.example', subscribed: false }] }); // typed with a slip at the gate
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: 'jane@gmial.example' }] }); // the same slip, as the trial recorded it
    const stripeMod = await import('../../src/services/stripe');
    const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    create.mockClear();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'jane@gmail.example', email_confirmed: 'on' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('email_mismatch');
    expect(create).not.toHaveBeenCalled();
    // The typed address was not looked up: the refusal says nothing about it.
    expect(mockQuery.mock.calls.some(([sql]) => /FROM customers WHERE email/.test(String(sql)))).toBe(false);
  });

  it('the form with no address at all uses the account\'s (and still needs the confirmation)', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild(buildRow({ customer_id: 'cust-preview-owner' }));
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'maya@example.com', subscribed: false }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: 'maya@example.com' }] }); // the address this trial gave
    const refused = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900' });
    expect(refused.status).toBe(400);
    expect(refused.body.error).toBe('email_confirmation_required');
  });

  it('a trial whose build belongs to an account for an address it never gave: no page, no Checkout, and that address is not disclosed', async () => {
    // An operator can dispatch a trial's build for an address of the
    // operator's choosing. The cookie is no claim on that account.
    const csrf = await csrfToken();
    const stripeMod = await import('../../src/services/stripe');
    const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    create.mockClear();
    for (const captured of [null, 'maya@example.com']) {
      setupTrialOwnedBuild(buildRow({ customer_id: 'cust-operator-chosen' }));
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'owner@elsewhere.example', subscribed: false }] });
      mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: captured }] });
      const page = await request(app)
        .get(`/lighthouse/lamp/${BUILD_ID}/make-public`)
        .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
      expect(page.status).toBe(409);
      expect(page.text).toContain('Please log in to that account');
      expect(page.text).not.toContain('owner@elsewhere.example');

      setupTrialOwnedBuild(buildRow({ customer_id: 'cust-operator-chosen' }));
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'owner@elsewhere.example', subscribed: false }] });
      mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: captured }] });
      const res = await request(app)
        .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
        .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
        .type('form')
        .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'owner@elsewhere.example', email_confirmed: 'on' });
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('account_login_required');
      expect(JSON.stringify(res.body)).not.toContain('owner@elsewhere.example');
    }
    expect(create).not.toHaveBeenCalled();
  });

  it('refuses to promote a trial into an account that already has a subscription', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild(buildRow({ customer_id: 'cust-already-keeper' }));
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'maya@example.com', subscribed: true }] }); // has a subscription
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: 'maya@example.com' }] }); // the address this trial gave
    const stripeMod = await import('../../src/services/stripe');
    const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    create.mockClear();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'maya@example.com' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('account_already_subscribed');
    expect(create).not.toHaveBeenCalled();
  });

  it('passes correct args to createHarborSubscriptionCheckoutSession', async () => {
    const csrf = await csrfToken();
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // no account has this email yet
    const stripeMod = await import('../../src/services/stripe');
    const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    create.mockClear();
    await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}${formSession}`)
      .type('form')
      .send({ _csrf: csrf, monthly_amount_cents: '2900', email: 'lena@example.com', sliding_scale_attestation: 'on', email_confirmed: 'on' });
    expect(create).toHaveBeenCalledOnce();
    const args = create.mock.calls[0][0];
    expect(args.buildId).toBe(BUILD_ID);
    expect(args.trialId).toBe(TRIAL_ID);
    expect(args.keeperCustomerId).toBeNull();
    expect(args.monthlyAmountCents).toBe(2900);
    expect(args.slidingScaleAttestation).toBe(true);
    // A trial has no Stripe customer on file, and none is made for it here:
    // Checkout creates one from this email when the session is paid.
    expect(args.stripeCustomerId).toBeNull();
    expect(args.customerEmail).toBe('lena@example.com');
  });

  describe('a logged-in keeper (the session customer owns the build)', () => {
    /** resolveLampOwnership path 1: the cookie lookup, then the build, owned by the session's customer. */
    async function keeperAgentWithCsrf() {
      const agent = await loginAsLighthouseAgent('keeper@example.com'); // session customerId = cust-1
      return { agent, csrf: (await import('../../src/middleware/auth')).generateCsrfToken({} as never) };
    }
    function setupKeeperOwnedBuild(): void {
      mockQuery.mockReset();
      setupTrialCookieMocks();
      mockQuery.mockResolvedValueOnce({ rows: [buildRow({ customer_id: 'cust-1' })] });
    }
    const post = (agent: supertest.Agent, csrf: string) =>
      agent
        .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
        .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
        .type('form')
        .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'keeper@example.com' });

    it('whose subscription already carries the right to publish is sent to the status page, and no second Checkout is opened', async () => {
      const { agent, csrf } = await keeperAgentWithCsrf();
      setupKeeperOwnedBuild();
      mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1' }] }); // keepers: subscribed
      const stripeMod = await import('../../src/services/stripe');
      const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
      create.mockClear();

      const res = await post(agent, csrf);
      expect(res.status).toBe(303);
      expect(res.headers.location).toBe(`/builds/${BUILD_ID}`);
      expect(create).not.toHaveBeenCalled();
      const asked = mockQuery.mock.calls.find(([sql]) => /FROM keepers\s+WHERE customer_id = \$1\s+AND stripe_subscription_id IS NOT NULL\s+AND subscription_status = ANY\(\$2::text\[\]\)/.test(String(sql)));
      expect(asked?.[1]).toEqual(['cust-1', ['active', 'trialing', 'past_due']]);
    });

    it('whose subscription has ended goes to Checkout on the Stripe customer already on file for them', async () => {
      const { agent, csrf } = await keeperAgentWithCsrf();
      setupKeeperOwnedBuild();
      mockQuery.mockResolvedValueOnce({ rows: [] }); // keepers: no subscription that publishes
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'keeper@example.com', subscribed: true }] }); // the keeper's own account
      mockQuery.mockResolvedValueOnce({ rows: [] }); // no unreleased trial is stamped with this build
      mockQuery.mockResolvedValueOnce({ rows: [{ stripe_customer_id: 'cus_on_file_1' }] }); // storedStripeCustomerId
      const stripeMod = await import('../../src/services/stripe');
      const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
      create.mockClear();

      const res = await post(agent, csrf);
      expect(res.status).toBe(303);
      expect(res.headers.location).toBe('https://checkout.stripe.com/mock-sub-session');
      expect(create).toHaveBeenCalledWith(
        expect.objectContaining({ stripeCustomerId: 'cus_on_file_1', keeperCustomerId: 'cust-1', trialId: null })
      );
    });

    it('with no Stripe customer on file (or only a development bypass id) gets one from Checkout, not from here', async () => {
      const { agent, csrf } = await keeperAgentWithCsrf();
      for (const stored of [null, 'cus_bypass_11111111']) {
        setupKeeperOwnedBuild();
        mockQuery.mockResolvedValueOnce({ rows: [] }); // not subscribed
        mockQuery.mockResolvedValueOnce({ rows: [{ email: 'keeper@example.com', subscribed: false }] }); // the keeper's own account
        mockQuery.mockResolvedValueOnce({ rows: [] }); // no unreleased trial is stamped with this build
        mockQuery.mockResolvedValueOnce({ rows: stored === null ? [] : [{ stripe_customer_id: stored }] });
        const stripeMod = await import('../../src/services/stripe');
        const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
        create.mockClear();
        const res = await post(agent, csrf);
        expect(res.status).toBe(303);
        expect(create).toHaveBeenCalledWith(
          expect.objectContaining({ stripeCustomerId: null, customerEmail: 'keeper@example.com' })
        );
      }
    });

    it('who logged in before paying: the conversation that built the site goes into the Checkout metadata, so the webhook promotes it', async () => {
      const { agent, csrf } = await keeperAgentWithCsrf();
      setupKeeperOwnedBuild();
      mockQuery.mockResolvedValueOnce({ rows: [] }); // no subscription yet
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'keeper@example.com', subscribed: false }] });
      mockQuery.mockResolvedValueOnce({ rows: [{ trial_id: TRIAL_ID }] }); // the unreleased trial stamped with this build
      mockQuery.mockResolvedValueOnce({ rows: [] }); // no Stripe customer on file
      const stripeMod = await import('../../src/services/stripe');
      const create = stripeMod.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
      create.mockClear();

      const res = await post(agent, csrf);
      expect(res.status).toBe(303);
      expect(create).toHaveBeenCalledWith(
        expect.objectContaining({ trialId: TRIAL_ID, keeperCustomerId: 'cust-1', customerEmail: 'keeper@example.com' })
      );
      const asked = mockQuery.mock.calls.find(([sql]) => /FROM trial_keepers\s+WHERE handoff_build_id = \$1 AND released_at IS NULL/.test(String(sql)));
      expect(asked?.[1]).toEqual([BUILD_ID]);
    });

    it('an account session needs no confirmation of its own address, and cannot pay under another one', async () => {
      const { agent, csrf } = await keeperAgentWithCsrf();
      setupKeeperOwnedBuild();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'keeper@example.com', subscribed: false }] });
      const res = await agent
        .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
        .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
        .type('form')
        .send({ _csrf: csrf, monthly_amount_cents: '3900', email: 'someone-else@example.com' });
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('email_mismatch');
    });
  });
});

describe('POST /lighthouse/lamp/:buildId/make-public is rate limited', () => {
  // Each accepted submission creates a Stripe Checkout session. The limiters
  // sit in front of the CSRF check and the handler, so the budget is spent by
  // requests of any kind. Built here on a router of its own with a budget of
  // two; the application's router uses MAKE_PUBLIC_MAX_PER_HOUR.
  it('the third submission from one trial cookie inside the window is refused with 429', async () => {
    const express = (await import('express')).default;
    const mod = await import('../../src/routes/lighthouse');
    const fakePool = { query: vi.fn().mockResolvedValue({ rows: [ACTIVE_TRIAL_ROW] }) };
    const limited = express();
    limited.use(express.urlencoded({ extended: false }));
    limited.use('/lighthouse', mod.createLighthouseRouter(fakePool as never, { makePublicMaxPerHour: 2 }));

    const submit = () =>
      request(limited)
        .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
        .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
        .type('form')
        .send({ monthly_amount_cents: '3900', email: 'maya@example.com' });

    // No CSRF token: the first two get as far as the CSRF check.
    expect((await submit()).status).toBe(403);
    expect((await submit()).status).toBe(403);
    const third = await submit();
    expect(third.status).toBe(429);
    expect(third.body.error).toBe('rate_limited');
  });

  it('the application default is ten an hour per trial cookie, in front of the CSRF check', async () => {
    const mod = await import('../../src/routes/lighthouse');
    expect(mod.MAKE_PUBLIC_MAX_PER_HOUR).toBe(10);
    const router = mod.createLighthouseRouter({ query: vi.fn() } as never);
    const layer = (router as unknown as {
      stack: Array<{ route?: { path: string; methods: Record<string, boolean>; stack: Array<{ name: string }> } }>;
    }).stack.find((l) => l.route?.path === '/lamp/:buildId/make-public' && l.route.methods.post);
    expect(layer).toBeDefined();
    // Two limiters (address, trial), the CSRF check, the handler. That the
    // limiters come first is what the test above observes: 403, 403, then 429.
    expect(layer!.route!.stack).toHaveLength(4);
  });
});

describe('GET /lighthouse/lamp/:buildId/published', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders settled state when build is live', async () => {
    setupTrialOwnedBuild(buildRow({ status: 'live', live_url: 'https://example.test' }));
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/published`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/website is public/i);
    expect(res.text).toContain('https://example.test');
  });

  it('renders almost-there state with aria-live polling block when webhook has not settled yet', async () => {
    setupTrialOwnedBuild();  // status=preview still
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/published`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Almost there/i);
    // Focus-reset hazard (WCAG 2.2.4): the page must NOT use a meta-refresh
    // that reloads every 3 seconds and resets document.activeElement.
    expect(res.text).not.toMatch(/<meta http-equiv="refresh"/i);
    // Replaced with an aria-live polite region + inline polling script so
    // screen-reader keepers get a status announcement without focus resets.
    expect(res.text).toMatch(/role="status"[^>]*aria-live="polite"/);
    expect(res.text).toMatch(/id="publish-status"/);
    expect(res.text).toMatch(/fetch\(window\.location\.href/);
  });
});

describe('GET /lighthouse/lamp/:buildId/cancelled', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders the cancellation landing without state mutation', async () => {
    setupTrialOwnedBuild();
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}/cancelled`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/Whenever you are ready/i);
  });
});

// ===== /lighthouse/lamp/:buildId/cancel-subscription (commit 3, 2026-04-28) =====
// Cancel-at-period-end gate. Keeper-only route; trial owners are pre-payment
// and have no subscription to cancel.

describe('POST /lighthouse/lamp/:buildId/cancel-subscription', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  /**
   * Mint a valid CSRF token by calling the in-process generator. The lamp
   * page only renders a CSRF form when the subscription is active+keeper-
   * owned, which is not what we want to exercise on the negative-path
   * tests below; minting independently keeps each test focused.
   */
  async function mintCsrfToken(): Promise<string> {
    const authMod = await import('../../src/middleware/auth');
    return authMod.generateCsrfToken({} as never);
  }

  it('rejects without CSRF (403)', async () => {
    setupTrialOwnedBuild();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/cancel-subscription`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
      .type('form')
      .send({});
    expect(res.status).toBe(403);
  });

  it('rejects trial owner with 403 keeper_required (no subscription on trial path)', async () => {
    const csrf = await mintCsrfToken();
    setupTrialOwnedBuild();
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/cancel-subscription`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
      .type('form')
      .send({ _csrf: csrf });
    expect(res.status).toBe(403);
    expect(res.body.error).toBe('keeper_required');
  });

  it('returns 404 for non-owner', async () => {
    const csrf = await mintCsrfToken();
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [buildRow()] });   // build exists
    mockQuery.mockResolvedValueOnce({ rows: [] });             // not trial-owned
    const res = await request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/cancel-subscription`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
      .type('form')
      .send({ _csrf: csrf });
    expect(res.status).toBe(404);
  });
});

// ===== Lamp live-state subscription affordances =====
// The view conditionally renders Cancel + past_due + paused affordances
// based on subscriptionState passed by the route. Trial-owned live builds
// pass subscriptionState=null so none of the affordances show.

// Regression for an e2e finding: after promotion, conversation
// turns are re-keyed from trial_id to keeper_id. /table must load by
// keeper_id when an authed keeper visits, otherwise the entire
// conversation history (and Haven's harbor blessing turn) is invisible.
describe('GET /lighthouse/table: promoted-keeper turn history regression', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('SELECTs companion_turns by keeper_id when session.customerId matches a keepers row', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    try {
      const agent = await loginAsLighthouseAgent('sage@example.com');
      // /table chain: trial-cookie middleware, keepers existence SELECT,
      // loadKeeperTurnHistory (NEW), initialMemory.
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });             // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [{ handoff_build_id: 'b-1' }] });  // keepers SELECT
      mockQuery.mockResolvedValueOnce({ rows: [{ status: 'preview', live_url: null }] }); // builds SELECT (live-site detection)
      mockQuery.mockResolvedValueOnce({                                          // loadKeeperTurnHistory
        rows: [
          { turn_id: 't1', keeper_text: 'hi', companion_text: 'hello', rail_flags: [], created_at: new Date() },
          { turn_id: 't2', keeper_text: null, companion_text: 'The lamp is set. Quiet your shoulders.', rail_flags: [], created_at: new Date() },
        ],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] });                             // initialMemory

      const res = await agent.get('/lighthouse/table');
      expect(res.status).toBe(200);
      // The keeper-keyed turn history loaded; blessing turn rendered.
      expect(res.text).toContain('Quiet your shoulders');
      // Verify the SQL chosen is the keeper_id variant (not trial_id).
      const turnHistoryCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM companion_turns') &&
          c[0].includes('WHERE keeper_id = $1')
      );
      expect(turnHistoryCall).toBeDefined();
      // And NOT the trial_id variant (would have been the bug).
      const trialKeyedCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM companion_turns') &&
          c[0].includes('WHERE trial_id = $1')
      );
      expect(trialKeyedCall).toBeUndefined();
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });

  it('falls back to trial_id-keyed turn history when keepers row is missing (pre-promotion)', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    try {
      // Logged-in agent but no keepers row yet (session.customerId not promoted).
      const agent = await loginAsLighthouseAgent('not-yet-promoted@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });   // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [] });                    // keepers SELECT empty
      mockQuery.mockResolvedValueOnce({ rows: [] });                    // loadTurnHistory by trial_id
      mockQuery.mockResolvedValueOnce({ rows: [] });                    // initialMemory

      const res = await agent.get('/lighthouse/table');
      expect(res.status).toBe(200);
      const turnHistoryCall = mockQuery.mock.calls.find(
        (c) =>
          typeof c[0] === 'string' &&
          c[0].includes('FROM companion_turns') &&
          c[0].includes('WHERE trial_id = $1')
      );
      expect(turnHistoryCall).toBeDefined();
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });
});

// 2026-04-29: admin-override regression. Founder hit a 500 "Build not
// found" trying to inspect a keeper's lamp without bypassing as them.
// resolveLampOwnership now accepts NODE_ENV !== 'production' AND
// req.session.customerEmail === ADMIN_EMAIL as a third path that
// adopts the build's keeper customer_id (so subscription_state, the
// $X/mo affordance, and the cancel form all show the right data).
describe('GET /lighthouse/lamp/:buildId (admin override)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('admin in dev sees any keeper-owned build without bypassing as them', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    const ORIGINAL_ADMIN = process.env.ADMIN_EMAIL;
    process.env.NODE_ENV = 'development';
    process.env.ADMIN_EMAIL = 'admin@example.com';
    try {
      const agent = await loginAsLighthouseAgent('admin@example.com');
      // GET /lamp/:id chain when admin reads a keeper-owned build:
      //   1. trial-cookie middleware (1 query)
      //   2. SELECT builds (build exists, customer_id set; admin auth
      //      path activates AFTER the trial-not-owning lookup)
      //   3. SELECT trial_keepers (returns empty: trial doesn't own)
      //   4. (admin path matches; no further query unless live)
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: BUILD_ID,
            customer_id: 'cust-keeper-1',
            app_name: 'Sample Reiki Studio',
            status: 'preview',
            review_status: 'approved',
            review_notes: null,
            preview_url: 'https://sample-reiki-studio.example.com',
            live_url: null,
          },
        ],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] }); // trial_keepers empty
      const res = await agent.get(`/lighthouse/lamp/${BUILD_ID}`);
      expect(res.status).toBe(200);
      expect(res.text).toMatch(/Sample Reiki Studio/);
      expect(res.text).toMatch(/Make it public/);
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      if (ORIGINAL_ADMIN === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = ORIGINAL_ADMIN;
    }
  });

  it('refuses admin override in production (no read of other keepers)', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    const ORIGINAL_ADMIN = process.env.ADMIN_EMAIL;
    process.env.NODE_ENV = 'production';
    process.env.ADMIN_EMAIL = 'admin@example.com';
    try {
      const agent = await loginAsLighthouseAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: BUILD_ID,
            customer_id: 'cust-other-keeper',
            app_name: 'Some other build',
            status: 'preview',
            review_status: 'approved',
            review_notes: null,
            preview_url: 'https://example.test',
            live_url: null,
          },
        ],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] }); // trial doesn't own
      const res = await agent.get(`/lighthouse/lamp/${BUILD_ID}`);
      expect(res.status).toBe(404);
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      if (ORIGINAL_ADMIN === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = ORIGINAL_ADMIN;
    }
  });

  it('refuses non-admin logged-in user even in dev', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    const ORIGINAL_ADMIN = process.env.ADMIN_EMAIL;
    process.env.NODE_ENV = 'development';
    process.env.ADMIN_EMAIL = 'admin@example.com';
    try {
      const agent = await loginAsLighthouseAgent('not-admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: BUILD_ID,
            customer_id: 'cust-other-keeper',
            app_name: 'Some other build',
            status: 'preview',
            review_status: 'approved',
            review_notes: null,
            preview_url: 'https://example.test',
            live_url: null,
          },
        ],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] }); // trial doesn't own
      const res = await agent.get(`/lighthouse/lamp/${BUILD_ID}`);
      expect(res.status).toBe(404);
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      if (ORIGINAL_ADMIN === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = ORIGINAL_ADMIN;
    }
  });
});

describe('GET /lighthouse/lamp/:buildId (live-state)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders live state without cancel form when ownerKind is trial (no subscription state)', async () => {
    setupTrialOwnedBuild(buildRow({ status: 'live', live_url: 'https://example.test' }));
    // No subscription lookups happen because ownerKind !== 'keeper'.
    const res = await request(app).get(`/lighthouse/lamp/${BUILD_ID}`).set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    // 2026-04-28: live-state eyebrow reframed from "The lamp is set" (metaphor)
    // to plain "Your website is public" per founder direction.
    expect(res.text).toMatch(/website is public/i);
    expect(res.text).not.toMatch(/Cancel subscription/);
  });
});

describe('POST /lighthouse/preview/choose', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  async function csrf(): Promise<string> {
    const authMod = await import('../../src/middleware/auth');
    return authMod.generateCsrfToken({} as never);
  }

  it('rejects a request without a CSRF token (403) and writes nothing', async () => {
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/preview/choose')
      .send({ template_family_id: 'editorial-statement' });
    expect(res.status).toBe(403);
    const insert = mockQuery.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('INSERT INTO keeper_memory')
    );
    expect(insert).toBeUndefined();
  });

  it('accepts the token in the x-csrf-token header (what preview.ejs sends)', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ memory_id: 'm-1' }] }); // INSERT keeper_memory
    const res = await request(app)
      .post('/lighthouse/preview/choose')
      .set('x-csrf-token', await csrf())
      .send({ template_family_id: 'editorial-statement' });
    expect(res.status).toBe(200);
  });

  it('persists the choice to keeper_memory and returns redirect', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ memory_id: 'm-1' }] }); // INSERT keeper_memory
    const res = await request(app)
      .post('/lighthouse/preview/choose')
      .set('x-csrf-token', await csrf())
      .send({ template_family_id: 'editorial-statement' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.template_family_id).toBe('editorial-statement');
    expect(res.body.redirect).toBe('/lighthouse/table');
    // Confirm the INSERT was queued with content carrying the choice.
    const insert = mockQuery.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('INSERT INTO keeper_memory')
    );
    expect(insert).toBeDefined();
    expect(insert![0]).toMatch(/category = 'design_pref'|'design_pref'/);
    expect(insert![1]?.[0]).toBe(TRIAL_ID);
    expect(JSON.parse(insert![1]?.[1])).toEqual({ template_family_id: 'editorial-statement' });
    // A trial's choice is written only while the trial is unreleased.
    expect(insert![0]).toMatch(/\(trial_id, partition/);
    expect(insert![0]).toMatch(/released_at IS NULL/);
  });

  it('a keeper\'s choice is written under the keeper, where the extraction reads it', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com'); // session customerId = cust-1
    mockQuery.mockReset();
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] }); // keepers row for the session
    mockQuery.mockResolvedValueOnce({ rows: [{ memory_id: 'm-2' }] }); // INSERT keeper_memory
    const res = await agent
      .post('/lighthouse/preview/choose')
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
      .set('x-csrf-token', await csrf())
      .send({ template_family_id: 'editorial-statement' });
    expect(res.status).toBe(200);
    const insert = mockQuery.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('INSERT INTO keeper_memory')
    );
    expect(insert![0]).toMatch(/\(keeper_id, partition/);
    expect(insert![1]?.[0]).toBe('cust-1');
  });

  it('answers 409 and says to reload when the owner changed under the request (nothing was written)', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // the trial was released in the meantime: the insert wrote no row
    const res = await request(app)
      .post('/lighthouse/preview/choose')
      .set('x-csrf-token', await csrf())
      .send({ template_family_id: 'editorial-statement' });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('conversation_moved');
  });

  it('rejects malformed template_family_id with 400', async () => {
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/preview/choose')
      .set('x-csrf-token', await csrf())
      .send({ template_family_id: 'NOT VALID!!' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('invalid_template_family_id');
  });

  it('rejects unknown template_family_id with 400', async () => {
    setupTrialCookieMocks();
    const res = await request(app)
      .post('/lighthouse/preview/choose')
      .set('x-csrf-token', await csrf())
      .send({ template_family_id: 'made-up-family' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('unknown_template_family');
  });

  // Note: no test for "missing trial context" because the trialCookie
  // middleware always either loads or mints a trial -> req.harborTrial
  // is always set in practice. The route's `if (!trial)` is a
  // defensive type-narrowing branch, not a reachable runtime path
  // unless trialCookie itself fails (in which case it throws to
  // express's error handler -> 500). The 'no_trial_context' JSON
  // shape is documented in the source for completeness.
});

// 2026-05-03: keepers with a live build had no in-product path to
// request a change. /lighthouse/table now checks build.status when the
// keeper has a handoff_build_id; if status='live' the view exposes a
// "Make changes to my site" CTA that pre-fills the textarea.
describe('GET /lighthouse/table: live-site change-request affordance', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders the "Make changes to my site" CTA when the keeper has a live build', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    try {
      const agent = await loginAsLighthouseAgent('keeper-live@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });            // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [{ handoff_build_id: 'b-live' }] }); // keepers SELECT
      mockQuery.mockResolvedValueOnce({                                          // builds SELECT
        rows: [{ status: 'live', live_url: 'https://sample-reiki-studio.example.com' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] });                             // loadKeeperTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] });                             // initialMemory

      const res = await agent.get('/lighthouse/table');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Make changes to my site');
      expect(res.text).toContain('change-request-affordance');
      // Live URL is linked.
      expect(res.text).toContain('https://sample-reiki-studio.example.com');
      // The "view status" link for the in-progress path should NOT
      // render alongside the live affordance.
      expect(res.text).not.toContain('Your website is being built');
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });

  it('still shows "view status" (not the change-request CTA) when the build is not yet live', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    try {
      const agent = await loginAsLighthouseAgent('keeper-pending@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });            // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [{ handoff_build_id: 'b-pending' }] }); // keepers SELECT
      mockQuery.mockResolvedValueOnce({                                          // builds SELECT
        rows: [{ status: 'preview', live_url: null }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] });                             // loadKeeperTurnHistory
      mockQuery.mockResolvedValueOnce({ rows: [] });                             // initialMemory

      const res = await agent.get('/lighthouse/table');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Your website is being built');
      expect(res.text).not.toContain('Make changes to my site');
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });
});

// ─────────────────────────────────────────────────────────────────────
// Skip-to-main-content link across standalone /lighthouse/* HTML docs
// (harbor-skip-to-content-on-standalone-docs, 2026-05-09).
//
// WCAG 2.1 AA section 2.4.1 (Bypass Blocks). Every standalone view
// renders the same shared partial (_harbor-skip-link.ejs) as the
// first child of <body>, paired with <main id="main"> wrapping the
// primary content. Visually hidden until focused so it doesn't
// disrupt the warm-paper aesthetic, then revealed at the top-left.
// ─────────────────────────────────────────────────────────────────────

describe('Skip-to-main-content link on /lighthouse/* standalone HTML docs', () => {
  function assertSkipLinkAndMain(html: string): void {
    // The visually-hidden anchor must point at #main.
    expect(html).toMatch(/<a[^>]*href="#main"[^>]*class="cas-skip-link"[^>]*>\s*Skip to main content\s*<\/a>/);
    // The styling rule that hides the link off-screen until focused.
    expect(html).toMatch(/\.cas-skip-link\s*\{[^}]*position:\s*absolute/);
    expect(html).toMatch(/\.cas-skip-link:focus\s*\{[^}]*top:\s*8px/);
    // The matching landing target. Tabbing to the skip link then
    // activating it should jump the keyboard to <main id="main">.
    expect(html).toMatch(/<main[^>]*id="main"/);
    // Skip link must precede the <main> wrapper in document order so
    // it is the first focusable thing on the page.
    const skipIdx = html.search(/<a[^>]*class="cas-skip-link"/);
    const mainIdx = html.search(/<main[^>]*id="main"/);
    expect(skipIdx).toBeGreaterThan(0);
    expect(mainIdx).toBeGreaterThan(skipIdx);
  }

  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('GET /lighthouse/table ships the skip link + <main id="main">', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // loadTurnHistory
    mockQuery.mockResolvedValueOnce({ rows: [] }); // initialMemory
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    assertSkipLinkAndMain(res.text);
  });

  it('GET /lighthouse/table/recall ships the skip link + <main id="main">', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table/recall');
    expect(res.status).toBe(200);
    assertSkipLinkAndMain(res.text);
  });

  it('GET /lighthouse/preview ships the skip link + <main id="main">', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // archetype lookup
    mockQuery.mockResolvedValueOnce({ rows: [] }); // pref lookup
    const res = await request(app).get('/lighthouse/preview');
    expect(res.status).toBe(200);
    assertSkipLinkAndMain(res.text);
  });

  it('GET /lighthouse/lamp/:buildId ships the skip link + <main id="main">', async () => {
    setupTrialOwnedBuild();
    const res = await request(app)
      .get(`/lighthouse/lamp/${BUILD_ID}`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    assertSkipLinkAndMain(res.text);
  });

  it('GET /lighthouse/lamp/:buildId/make-public ships the skip link + <main id="main">', async () => {
    setupTrialOwnedBuild();
    mockQuery.mockResolvedValueOnce({ rows: [{ email_captured: 'maya@example.com' }] });
    const res = await request(app)
      .get(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    assertSkipLinkAndMain(res.text);
  });

  it('GET /lighthouse/lamp/:buildId/make-public (changes_requested 409) ships the skip link + <main id="main">', async () => {
    setupTrialOwnedBuild(buildRow({ review_status: 'changes_requested', review_notes: 'Tighten home.' }));
    const res = await request(app)
      .get(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(409);
    assertSkipLinkAndMain(res.text);
  });

  it('GET /lighthouse/lamp/:buildId/published ships the skip link + <main id="main">', async () => {
    setupTrialOwnedBuild(buildRow({ status: 'live', live_url: 'https://example.com' }));
    const res = await request(app)
      .get(`/lighthouse/lamp/${BUILD_ID}/published`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    assertSkipLinkAndMain(res.text);
  });

  it('GET /lighthouse/lamp/:buildId/cancelled ships the skip link + <main id="main">', async () => {
    setupTrialOwnedBuild();
    const res = await request(app)
      .get(`/lighthouse/lamp/${BUILD_ID}/cancelled`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`);
    expect(res.status).toBe(200);
    assertSkipLinkAndMain(res.text);
  });
});

// ===========================================================================
// R1-W2 review additions
// ===========================================================================

describe('GET /lighthouse/table: ?dev_trial= is never reflected unvalidated (XSS)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  function queueEmptyTable(): void {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // turn history
    mockQuery.mockResolvedValueOnce({ rows: [] }); // initial memory
    mockQuery.mockResolvedValueOnce({ rows: [] }); // self-serve guard
  }

  it('drops a script-breaking dev_trial value instead of echoing it into the page', async () => {
    queueEmptyTable();
    // encodeURIComponent leaves ' - ( ) unescaped, which is enough to turn a
    // raw echo inside a single-quoted JS string into code execution.
    const payload = "x'-alert(document.domain)-'";
    const res = await request(app).get('/lighthouse/table').query({ dev_trial: payload });
    expect(res.status).toBe(200);
    expect(res.text).not.toContain('alert(document.domain)');
    expect(res.text).not.toContain('dev_trial');
  });

  it('keeps a well-formed UUID dev_trial in the links and in the script data attribute', async () => {
    queueEmptyTable();
    const uuid = '99999999-aaaa-bbbb-cccc-dddddddddddd';
    const res = await request(app).get('/lighthouse/table').query({ dev_trial: uuid });
    expect(res.status).toBe(200);
    expect(res.text).toContain(`href="/lighthouse/table/recall?dev_trial=${uuid}"`);
    expect(res.text).toContain(
      `data-recall-json-url="/lighthouse/table/recall?format=json&amp;dev_trial=${uuid}"`
    );
  });

  it('the page script reads the recall URL from the attribute, not from an inline echo', async () => {
    queueEmptyTable();
    const res = await request(app).get('/lighthouse/table');
    expect(res.text).toContain("conversation.getAttribute('data-recall-json-url')");
    expect(res.text).not.toMatch(/fetch\('\/lighthouse\/table\/recall\?format=json[^']/);
  });
});

describe('GET /lighthouse/table: screen-reader contract (audit SR1/SR4/SR5/SR6)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('wraps the conversation in a polite log live region with an accessible name', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    const section = res.text.match(/<section class="conversation" id="conversation"[^>]*>/);
    expect(section).not.toBeNull();
    expect(section![0]).toContain('role="log"');
    expect(section![0]).toContain('aria-live="polite"');
    expect(section![0]).toContain('aria-atomic="false"');
    expect(section![0]).toContain('aria-label="Conversation with Haven"');
  });

  it('the injected email-gate form lives inside the live region and its text is set via textContent', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    // appendEmailGate() appends to `conversation` (the role="log" section)...
    expect(res.text).toMatch(/conversation\.appendChild\(wrap\);\s*scrollToBottom\(\);\s*var input = document\.getElementById\('email-gate-input'\)/);
    // ...and the server-supplied gate message is never concatenated into HTML.
    expect(res.text).toContain('gateText.textContent = message');
    expect(res.text).not.toMatch(/'<p>' \+ \(message/);
  });
});

describe('POST /lighthouse/build: keeper path (promoted keeper with a session)', () => {
  // The route hands the keeper path to dispatchHarborKeeperBuild in
  // services/harbor_dispatch.ts. That function is the real one here (only the
  // trial function of the module is replaced by a double), so these tests run
  // the route and the shared service together against a database double.
  let mockConnect: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    mockQuery.mockReset();
    mockExtract.mockReset();
    mockDispatch.mockReset();
    const poolModule = await import('../../src/db/index');
    mockConnect = (poolModule.default.connect as unknown) as ReturnType<typeof vi.fn>;
    mockConnect.mockReset();
  });

  /** An extraction with one real thing the keeper said, so the barren-spec guard lets it through. */
  function stubExtraction(over: Record<string, unknown> = {}): void {
    mockExtract.mockResolvedValue({
      spec: {
        spec_version: '1.0',
        app_name: 'sample-reiki-studio',
        tier: 'build',
        copy: { about_body: 'I teach reiki in Portland.' },
        pages: [],
        ...over,
      },
      warnings: [],
      costMicros: 1234,
      contextSize: { memoryRows: 3, turns: 6 },
    });
  }

  const KEEPER_LOCK = /SELECT handoff_build_id, handoff_at FROM keepers WHERE customer_id = \$1 FOR UPDATE/;

  function stubClient(queryImpl: (sql: string) => { rows: unknown[] }) {
    const calls: string[] = [];
    const params: unknown[][] = [];
    const client = {
      query: vi.fn(async (sql: string, values?: unknown[]) => {
        calls.push(sql);
        params.push(values ?? []);
        return queryImpl(sql);
      }),
      release: vi.fn(),
    };
    mockConnect.mockResolvedValue(client);
    return { client, calls, params };
  }

  it('a logged-in customer who is not a keeper builds the preview of the conversation under their cookie, into their own account', async () => {
    // Their conversation page offers "Build my free preview" (they are not a
    // keeper, so it shows the trial's conversation); the button has to work.
    const agent = await loginAsLighthouseAgent('nobody@example.com'); // session customerId = cust-1
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
    mockQuery.mockResolvedValueOnce({ rows: [] }); // keepers SELECT: not a keeper
    mockQuery.mockResolvedValueOnce({
      rows: [{ is_synthetic: false, email_captured: 'typed-earlier@example.com', handoff_build_id: null, handoff_at: null }],
    }); // trial guard
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'Nobody@Example.com' }] }); // the session account's address
    mockQuery.mockResolvedValueOnce({ rows: [{ c: '9' }] }); // turn count (ready)
    mockDispatch.mockResolvedValueOnce({ buildId: 'b-own', alreadyDispatched: false, warnings: [] });

    const res = await agent.post('/lighthouse/build').set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`).send({ email: 'someone-else@example.com' });
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ buildId: 'b-own', redirect: '/lighthouse/lamp/b-own' });
    // The account is the one the session proves: nothing typed, now or
    // earlier, decides it, and a verified account is allowed because it is theirs.
    expect(mockDispatch.mock.calls[0][1]).toMatchObject({
      trialId: TRIAL_ID,
      resolvedEmail: 'nobody@example.com',
      allowVerifiedAccount: true,
      anonymous: false,
    });
    const asked = mockQuery.mock.calls.find(([sql]) => /SELECT email FROM customers WHERE id = \$1/.test(String(sql)));
    expect(asked?.[1]).toEqual(['cust-1']);
  });

  it('is idempotent when a build is already attached (no extraction, no insert)', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ customer_id: 'cust-1', handoff_build_id: 'build-existing', handoff_at: new Date() }],
    });
    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ buildId: 'build-existing', alreadyDispatched: true });
    expect(mockExtract).not.toHaveBeenCalled();
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it('a keeper whose build was dispatched and has since been deleted is refused, before any extraction', async () => {
    // handoff_build_id is ON DELETE SET NULL; handoff_at is what remembers.
    const agent = await loginAsLighthouseAgent('keeper@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ customer_id: 'cust-1', handoff_build_id: null, handoff_at: new Date('2026-05-01T00:00:00Z') }],
    });
    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('build_already_used');
    expect(mockExtract).not.toHaveBeenCalled();
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it('a keeper whose account address the build engine would refuse gets no build: refused before the limits are read and before any extraction', async () => {
    const { OWNER_EMAIL_NOT_BUILDABLE_MESSAGE } = await import('../../src/services/harbor_dispatch');
    const agent = await loginAsLighthouseAgent("o'brien@example.com");
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ customer_id: 'cust-1', handoff_build_id: null, handoff_at: null, email: "o'brien@example.com" }],
    });
    const before = mockQuery.mock.calls.length;
    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body).toEqual({ error: 'email_not_buildable', message: OWNER_EMAIL_NOT_BUILDABLE_MESSAGE });
    // Two statements: the trial cookie and the keeper row (with the account's email).
    const ran = mockQuery.mock.calls.slice(before).map(([sql]) => String(sql));
    expect(ran).toHaveLength(2);
    expect(ran[1]).toMatch(/FROM keepers k\s+JOIN customers c ON c\.id = k\.customer_id/);
    expect(ran.some((sql) => /build_start_events/.test(sql))).toBe(false);
    expect(mockExtract).not.toHaveBeenCalled();
    expect(mockConnect).not.toHaveBeenCalled();
    expect(mockDispatch).not.toHaveBeenCalled();
  });

  it('locks the keeper row (FOR UPDATE) before inserting the build as a Harbor build, in one transaction', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1', handoff_build_id: null, handoff_at: null, email: 'keeper@example.com' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ active: 0, starts_today: 0 }] }); // build-start counts, before the extraction
    stubExtraction();
    const { client, calls, params } = stubClient((sql) => {
      if (KEEPER_LOCK.test(sql)) return { rows: [{ handoff_build_id: null, handoff_at: null }] };
      if (/INSERT INTO builds/.test(sql)) return { rows: [{ id: 'build-new' }] };
      return { rows: [] };
    });

    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ buildId: 'build-new', alreadyDispatched: false });
    // The extraction was keyed on the keeper, not on the trial cookie.
    expect(mockExtract).toHaveBeenCalledWith(expect.objectContaining({ keeperCustomerId: 'cust-1' }));
    expect(calls[0]).toBe('BEGIN');
    // The customer's build-start lock first (the one routes/builds.ts takes), then the row.
    expect(calls[1]).toMatch(/pg_advisory_xact_lock\(hashtext\('cas-build-start'\), hashtext\(\$1\)\)/);
    expect(params[1]).toEqual(['cust-1']);
    expect(calls[2]).toMatch(KEEPER_LOCK);
    const insertAt = calls.findIndex((c) => /INSERT INTO builds /.test(c));
    expect(insertAt).toBeGreaterThan(2);
    // The shared insert: queued, and marked is_harbor so its publish needs a subscription.
    expect(calls[insertAt]).toMatch(
      /\(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)\s+VALUES \(\$1, \$2, 'queued', \$3, \$4, TRUE, \$5\)/
    );
    expect(params[insertAt][0]).toBe('cust-1');
    // The stamp sets the id and the durable handoff_at together.
    const stampAt = calls.findIndex((c) => /UPDATE keepers/.test(c));
    expect(stampAt).toBeGreaterThan(insertAt);
    expect(calls[stampAt]).toMatch(/SET handoff_build_id = \$1, handoff_at = NOW\(\), updated_at = NOW\(\)\s+WHERE customer_id = \$2 AND handoff_build_id IS NULL/);
    expect(params[stampAt]).toEqual(['build-new', 'cust-1']);
    // The start is recorded in the ledger the rebuild and revision limits read.
    const ledgerAt = calls.findIndex((c) => /INSERT INTO build_start_events/.test(c));
    expect(ledgerAt).toBeGreaterThan(stampAt);
    expect(params[ledgerAt].slice(0, 2)).toEqual(['cust-1', 'build-new']);
    expect(calls[calls.length - 1]).toBe('COMMIT');
    expect(client.release).toHaveBeenCalledTimes(1);
  });

  it('a concurrent loser returns the winner\'s build and inserts nothing', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    // The unlocked read still sees no stamp (both clicks passed it)...
    mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1', handoff_build_id: null, handoff_at: null, email: 'keeper@example.com' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ active: 0, starts_today: 0 }] }); // build-start counts, before the extraction
    stubExtraction();
    // ...but under the row lock the winner's stamp is visible.
    const { calls } = stubClient((sql) => {
      if (KEEPER_LOCK.test(sql)) {
        return { rows: [{ handoff_build_id: 'build-winner', handoff_at: new Date() }] };
      }
      return { rows: [] };
    });

    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ buildId: 'build-winner', alreadyDispatched: true });
    expect(calls.some((c) => /INSERT INTO builds/.test(c))).toBe(false);
    expect(calls[calls.length - 1]).toBe('COMMIT');
  });

  it('the one-build rule is applied again under the lock: a build dispatched and deleted in the meantime still refuses', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1', handoff_build_id: null, handoff_at: null, email: 'keeper@example.com' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ active: 0, starts_today: 0 }] }); // build-start counts, before the extraction
    stubExtraction();
    const { calls } = stubClient((sql) => {
      if (KEEPER_LOCK.test(sql)) return { rows: [{ handoff_build_id: null, handoff_at: new Date() }] };
      return { rows: [] };
    });

    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('build_already_used');
    expect(calls.some((c) => /INSERT INTO builds/.test(c))).toBe(false);
    expect(calls).toContain('ROLLBACK');
  });

  it('refuses a conversation with nothing in it (the guard the trial path already had)', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1', handoff_build_id: null, handoff_at: null, email: 'keeper@example.com' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ active: 0, starts_today: 0 }] }); // build-start counts, before the extraction
    stubExtraction({ copy: {} });

    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('barren_spec');
    expect(mockConnect).not.toHaveBeenCalled();
  });

  it('rolls back and surfaces the error when the insert fails', async () => {
    const agent = await loginAsLighthouseAgent('keeper@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    mockQuery.mockResolvedValueOnce({ rows: [{ customer_id: 'cust-1', handoff_build_id: null, handoff_at: null, email: 'keeper@example.com' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ active: 0, starts_today: 0 }] }); // build-start counts, before the extraction
    stubExtraction();
    const calls: string[] = [];
    const client = {
      query: vi.fn(async (sql: string) => {
        calls.push(sql);
        if (KEEPER_LOCK.test(sql)) return { rows: [{ handoff_build_id: null, handoff_at: null }] };
        if (/INSERT INTO builds/.test(sql)) throw new Error('insert failed');
        return { rows: [] };
      }),
      release: vi.fn(),
    };
    mockConnect.mockResolvedValue(client);

    const res = await agent.post('/lighthouse/build');
    expect(res.status).toBe(500);
    expect(calls).toContain('ROLLBACK');
    expect(client.release).toHaveBeenCalledTimes(1);
  });
});

describe('admin dev override is read-only on the lamp (R1-W2 review)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  async function csrfToken(): Promise<string> {
    const authMod = await import('../../src/middleware/auth');
    return authMod.generateCsrfToken({} as never);
  }

  function keeperOwnedBuild() {
    return {
      id: BUILD_ID,
      customer_id: 'cust-keeper-1',
      app_name: 'Sample Reiki Studio',
      status: 'preview',
      review_status: 'approved',
      review_notes: null,
      preview_url: 'https://sample-reiki-studio.example.com',
      live_url: null,
    };
  }

  async function withAdminEnv<T>(fn: (agent: supertest.Agent) => Promise<T>): Promise<T> {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    const ORIGINAL_ADMIN = process.env.ADMIN_EMAIL;
    process.env.NODE_ENV = 'development';
    process.env.ADMIN_EMAIL = 'admin@example.com';
    try {
      const agent = await loginAsLighthouseAgent('admin@example.com');
      return await fn(agent);
    } finally {
      process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      if (ORIGINAL_ADMIN === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = ORIGINAL_ADMIN;
    }
  }

  it('an admin session cannot open a Stripe Checkout for someone else\'s build', async () => {
    const stripe = await import('../../src/services/stripe');
    const checkout = stripe.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    checkout.mockClear();
    await withAdminEnv(async (agent) => {
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] }); // trial-cookie
      mockQuery.mockResolvedValueOnce({ rows: [keeperOwnedBuild()] }); // builds
      mockQuery.mockResolvedValueOnce({ rows: [] }); // trial does not own it
      const res = await agent
        .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
        .type('form')
        .send({ _csrf: await csrfToken(), monthly_amount_cents: '3900', email: 'a@example.com' });
      expect(res.status).toBe(404);
      expect(checkout).not.toHaveBeenCalled();
    });
  });

  it('an admin session cannot cancel someone else\'s subscription', async () => {
    const stripe = await import('../../src/services/stripe');
    const cancel = stripe.cancelHarborSubscriptionAtPeriodEnd as unknown as ReturnType<typeof vi.fn>;
    cancel.mockClear();
    await withAdminEnv(async (agent) => {
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      mockQuery.mockResolvedValueOnce({ rows: [keeperOwnedBuild()] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post(`/lighthouse/lamp/${BUILD_ID}/cancel-subscription`)
        .type('form')
        .send({ _csrf: await csrfToken() });
      expect(res.status).toBe(404);
      expect(cancel).not.toHaveBeenCalled();
    });
  });

  it('the same admin session can still READ the build via GET (the documented dev override)', async () => {
    await withAdminEnv(async (agent) => {
      mockQuery.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      mockQuery.mockResolvedValueOnce({ rows: [keeperOwnedBuild()] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get(`/lighthouse/lamp/${BUILD_ID}`);
      expect(res.status).toBe(200);
      expect(res.text).toMatch(/Sample Reiki Studio/);
    });
  });
});

describe('POST /lighthouse/lamp/:buildId/make-public: amount validation (R1-W2 review)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  async function post(amount: string | number) {
    const authMod = await import('../../src/middleware/auth');
    setupTrialOwnedBuild();
    return request(app)
      .post(`/lighthouse/lamp/${BUILD_ID}/make-public`)
      .set('Cookie', `cas_harbor_trial=${VALID_TOKEN}`)
      .type('form')
      .send({
        _csrf: authMod.generateCsrfToken({} as never),
        monthly_amount_cents: String(amount),
        sliding_scale_attestation: 'on',
        email: 'maya@example.com',
        email_confirmed: 'on',
      });
  }

  it('rejects an amount that is not a whole number of dollars', async () => {
    const res = await post(2550);
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('invalid_amount');
  });

  it('rejects trailing junk that parseInt would have accepted', async () => {
    expect((await post('2500abc')).status).toBe(400);
    expect((await post('25.5')).status).toBe(400);
    expect((await post('')).status).toBe(400);
  });

  it('still accepts an in-band whole-dollar amount', async () => {
    const stripe = await import('../../src/services/stripe');
    const checkout = stripe.createHarborSubscriptionCheckoutSession as unknown as ReturnType<typeof vi.fn>;
    checkout.mockClear();
    mockQuery.mockResolvedValue({ rows: [] }); // no account has this email yet
    const res = await post(2500);
    expect(res.status).toBe(303);
    expect(checkout).toHaveBeenCalledWith(expect.objectContaining({ monthlyAmountCents: 2500 }));
  });
});

describe('long conversations show the most recent turns (R1-W2 review)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('turn history selects the NEWEST 200 rows and re-sorts them oldest first', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    await request(app).get('/lighthouse/table');
    const historyCall = mockQuery.mock.calls.find(
      (c) => typeof c[0] === 'string' && c[0].includes('FROM companion_turns') && c[0].includes('LIMIT 200')
    );
    expect(historyCall).toBeDefined();
    const sql = historyCall![0] as string;
    const inner = sql.slice(sql.indexOf('FROM ('));
    expect(inner).toMatch(/ORDER BY created_at DESC\s+LIMIT 200/);
    expect(sql.trim()).toMatch(/\) recent\s+ORDER BY created_at ASC$/);
  });
});

describe('Harbor build / turn endpoints are wired to the per-address limiters (R1-W2 review)', () => {
  it('POST /table/turn: crisisFirst, then the cookie lookup, then the two limiters and the handler', async () => {
    const mod = await import('../../src/routes/lighthouse');
    const router = mod.createLighthouseRouter({ query: vi.fn() } as never);
    const stack = (router as unknown as {
      stack: Array<{ name: string; route?: { path: string; stack: Array<{ name: string }> } }>;
    }).stack;
    const turnLayers = stack.filter((l) => l.route?.path === '/table/turn');
    expect(turnLayers).toHaveLength(2);
    // First registration: the crisis handler alone (what it does is covered
    // in routes.lighthouse.crisis-first.test.ts).
    expect(turnLayers[0].route!.stack).toHaveLength(1);
    // Second: ip limiter, trial limiter, handler.
    expect(turnLayers[1].route!.stack).toHaveLength(3);
    // And the order in the router is crisis, cookie middleware, limiters.
    const crisisAt = stack.indexOf(turnLayers[0]);
    const cookieAt = stack.findIndex((l) => l.name === 'trialCookieHandler');
    const limitedAt = stack.indexOf(turnLayers[1]);
    expect(crisisAt).toBe(0);
    expect(cookieAt).toBeGreaterThan(crisisAt);
    expect(limitedAt).toBeGreaterThan(cookieAt);
  });

  it('POST /build runs two limiters before the handler', async () => {
    const mod = await import('../../src/routes/lighthouse');
    const router = mod.createLighthouseRouter({ query: vi.fn() } as never);
    const layer = (router as unknown as { stack: Array<{ route?: { path: string; stack: Array<{ name: string }> } }> })
      .stack.find((l) => l.route?.path === '/build');
    expect(layer).toBeDefined();
    expect(layer!.route!.stack).toHaveLength(3);
  });
});

describe('internal rail flags are not printed under the keeper\'s conversation (R1-W2 review)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('a crisis-flagged turn renders without a visible "crisis, suicide" label', async () => {
    setupTrialCookieMocks();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          turn_id: 't-crisis',
          keeper_text: 'a hard message',
          companion_text: 'Please reach 988.',
          rail_flags: ['crisis', 'suicide'],
          created_at: new Date('2026-06-01T10:00:00Z'),
        },
      ],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/lighthouse/table');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Please reach 988.');
    expect(res.text).not.toContain('class="turn-rail-flag"');
    expect(res.text).not.toContain('crisis, suicide');
    // The client appender no longer builds the label either.
    expect(res.text).not.toContain("span.className = 'turn-rail-flag'");
  });
});
