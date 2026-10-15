// tests/unit/routes.webhooks.test.ts
// POST /webhooks/build-status, POST /webhooks/stripe and POST /webhooks/resend.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import crypto from 'crypto';
import request from 'supertest';
import app from '../../src/app';
import { verifySvixSignature, resetEventOrderingCache } from '../../src/routes/webhooks';

// The database pool. pool.query is a plain mock (the build-status and Resend
// routes use it). pool.connect() hands out one client whose query function is
// mockClientQuery: the Stripe route runs each event in a transaction on that
// client, and the Stripe tests below point it at a small SQL-matching fake.
const mockClientQuery = vi.hoisted(() => vi.fn());
const mockClientRelease = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn().mockResolvedValue({ rows: [], rowCount: 0 });
  return {
    default: {
      query: mockQuery,
      connect: vi.fn(() =>
        Promise.resolve({
          query: mockClientQuery,
          release: mockClientRelease,
        })
      ),
      on: vi.fn(),
    },
  };
});

// Email service: the tests assert which of these are called, and when.
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildQueuedEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
  sendPreviewReadyEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn((email: string) => `token-for-${email}`),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
  sendHarborMadePublicEmail: vi.fn().mockResolvedValue(undefined),
  sendCrisisAlertEmail: vi.fn().mockResolvedValue(undefined),
  sendPaymentNotAppliedEmail: vi.fn().mockResolvedValue(undefined),
}));

// services/keeper_promotion is deliberately NOT mocked: the Stripe handler
// runs the real promotion steps on the transaction client, so the tests see
// the statements that release the trial inside the event's transaction.

vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));

// Stripe: the /stripe endpoint verifies the signature through constructWebhookEvent.
vi.mock('../../src/services/stripe', () => ({
  createHarborSubscriptionCheckoutSession: vi.fn(),
  cancelHarborSubscriptionAtPeriodEnd: vi.fn(),
  constructWebhookEvent: vi.fn(),
}));

vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello!'),
}));

import pool from '../../src/db/index';
import {
  sendAppLiveEmail,
  sendBuildFailedEmail,
  sendBuildQueuedEmail,
  sendPreviewReadyEmail,
  sendHarborMadePublicEmail,
  sendPaymentNotAppliedEmail,
} from '../../src/services/email';
import { constructWebhookEvent } from '../../src/services/stripe';
import {
  STRIPE_SUBSCRIPTION_STATUSES,
  PUBLISHING_SUBSCRIPTION_STATUSES,
  keeperSubscriptionStatus,
} from '../../src/services/keeper_promotion';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const mockPoolConnect = pool.connect as ReturnType<typeof vi.fn>;
const mockSendAppLiveEmail = sendAppLiveEmail as ReturnType<typeof vi.fn>;
const mockSendBuildFailedEmail = sendBuildFailedEmail as ReturnType<typeof vi.fn>;
const mockSendBuildQueuedEmail = sendBuildQueuedEmail as ReturnType<typeof vi.fn>;
const mockSendPreviewReadyEmail = sendPreviewReadyEmail as ReturnType<typeof vi.fn>;
const mockSendHarborMadePublicEmail = sendHarborMadePublicEmail as ReturnType<typeof vi.fn>;
const mockSendPaymentNotAppliedEmail = sendPaymentNotAppliedEmail as ReturnType<typeof vi.fn>;
const mockConstructWebhookEvent = constructWebhookEvent as ReturnType<typeof vi.fn>;

// Set WORKER_WEBHOOK_SECRET for auth (now mandatory)
process.env.WORKER_WEBHOOK_SECRET = 'test-webhook-secret';
// A stand-in signing secret; the signature check itself is mocked here and
// exercised for real in stripe.webhook-signature.test.ts.
const TEST_WEBHOOK_SECRET = ['whsec', 'unit', 'test'].join('_');

// --- POST /webhooks/build-status ---

describe('POST /webhooks/build-status', () => {
  beforeEach(() => {
    mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
    mockSendAppLiveEmail.mockReset().mockResolvedValue(undefined);
    mockSendBuildFailedEmail.mockReset().mockResolvedValue(undefined);
    mockSendBuildQueuedEmail.mockReset().mockResolvedValue(undefined);
    mockSendPreviewReadyEmail.mockReset().mockResolvedValue(undefined);
  });

  it('returns 400 when build_id is missing', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ status: 'live' });

    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/build_id/);
  });

  it('returns 400 when status is missing', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'b-1' });

    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/status/);
  });

  it('returns 400 when status is not an allowed value', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'b-1', status: 'deployed' });

    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/Invalid status/);
  });

  it('returns 404 when build_id does not match any build', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'no-such-build', status: 'building' });

    expect(res.status).toBe(404);
    expect(res.body.error).toMatch(/not found/i);
  });

  it('rejects status "live": a build becomes live only through the human approve route', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'build-1', status: 'live', live_url: 'https://myapp.up.railway.app' });

    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/Invalid status/);
    // Refused before any database access, and no "your site is live" mail.
    expect(mockQuery).not.toHaveBeenCalled();
    expect(mockSendAppLiveEmail).not.toHaveBeenCalled();
  });

  it('rejects statuses that merely inherit from Object.prototype', async () => {
    for (const status of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
      const res = await request(app)
        .post('/webhooks/build-status')
        .set('Authorization', 'Bearer test-webhook-secret')
        .send({ build_id: 'build-1', status });
      expect(res.status, status).toBe(400);
    }
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('calls sendBuildFailedEmail when status is "failed"', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'build-2',
        app_name: 'BrokenApp',
        current_status: 'building',
        email: 'dev@example.com',
        name: 'Bob',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'build-2', status: 'failed' });

    expect(res.status).toBe(200);
    await new Promise(r => setTimeout(r, 10));

    expect(mockSendBuildFailedEmail).toHaveBeenCalledOnce();
    expect(mockSendBuildFailedEmail).toHaveBeenCalledWith(
      'dev@example.com',
      'Bob',
      'BrokenApp'
    );
    expect(mockSendAppLiveEmail).not.toHaveBeenCalled();
  });

  it('does not send any email for intermediate status updates (e.g., "building")', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'build-3',
        app_name: 'WorkInProgress',
        current_status: 'queued',
        email: 'dev@example.com',
        name: 'Carol',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'build-3', status: 'building' });

    expect(res.status).toBe(200);
    await new Promise(r => setTimeout(r, 10));

    expect(mockSendAppLiveEmail).not.toHaveBeenCalled();
    expect(mockSendBuildFailedEmail).not.toHaveBeenCalled();
  });

  it('calls sendPreviewReadyEmail when status is "preview" and live_url is provided', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'build-preview-1',
        app_name: 'PreviewApp',
        current_status: 'deploying',
        email: 'user@example.com',
        name: 'Dan',
      }],
    });
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'build-preview-1', status: 'preview', live_url: 'https://preview.up.railway.app' });

    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);

    await new Promise(r => setTimeout(r, 10));

    expect(mockSendPreviewReadyEmail).toHaveBeenCalledOnce();
    expect(mockSendPreviewReadyEmail).toHaveBeenCalledWith(
      'user@example.com',
      'Dan',
      'PreviewApp',
      'https://preview.up.railway.app',
      'build-preview-1',
      'user@example.com'
    );
    expect(mockSendAppLiveEmail).not.toHaveBeenCalled();
  });

  it('returns 401 when Authorization header is wrong', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer wrongsecret')
      .send({ build_id: 'b-1', status: 'live' });

    expect(res.status).toBe(401);
  });

  it('returns 401 (not a thrown RangeError) for a same-length header with non-ASCII bytes', async () => {
    // Same number of characters as the real header, more bytes once encoded:
    // a length check on the strings followed by timingSafeEqual on the
    // buffers used to throw here.
    const expected = 'Bearer test-webhook-secret';
    const sameLength = 'Bearer ' + '\u00e9'.repeat(expected.length - 'Bearer '.length);
    expect(sameLength.length).toBe(expected.length);

    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', sameLength)
      .send({ build_id: 'b-1', status: 'live' });

    expect(res.status).toBe(401);
    expect(res.body.error).toBe('Unauthorized.');
  });

  it('rejects a live_url that is not http(s)', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'b-1', status: 'live', live_url: 'javascript:alert(1)' });

    expect(res.status).toBe(400);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('treats non-string fields as missing instead of passing them to SQL', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: { $ne: null }, status: ['live'] });

    expect(res.status).toBe(400);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  describe('every status write is a guarded UPDATE (cannot move a build backwards or resurrect one)', () => {
    const lookup = (currentStatus: string) => ({
      rows: [{ id: 'build-g', app_name: 'GuardedApp', current_status: currentStatus, email: 'g@example.com', name: 'G' }],
    });
    /** The UPDATE the handler issued, as [sql, params]. */
    const updateCall = (): [string, unknown[]] => {
      const call = mockQuery.mock.calls.find(([sql]) => /^\s*UPDATE builds/i.test(String(sql)));
      expect(call, 'the handler should have issued an UPDATE builds').toBeDefined();
      return [String(call![0]), call![1] as unknown[]];
    };

    it.each(['building', 'testing', 'deploying', 'preview', 'failed'])(
      '%s: the UPDATE only applies to an in-flight build, never to preview, live, failed or abandoned',
      async (status) => {
        mockQuery.mockResolvedValueOnce(lookup('building'));
        mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

        const res = await request(app)
          .post('/webhooks/build-status')
          .set('Authorization', 'Bearer test-webhook-secret')
          .send({ build_id: 'build-g', status });
        expect(res.status).toBe(200);

        const [sql, params] = updateCall();
        expect(sql).toMatch(/AND status = ANY\(\$\d+::text\[\]\)/);
        const allowedFrom = params.find(Array.isArray) as string[];
        expect(allowedFrom.length).toBeGreaterThan(0);
        for (const finished of ['preview', 'live', 'failed', 'abandoned', 'intake', 'payment_pending']) {
          expect(allowedFrom, `${status} must not start from ${finished}`).not.toContain(finished);
        }
        // The new status itself is written; it is not decided by the caller's SQL.
        if (status === 'preview') expect(sql).toMatch(/SET status = 'preview'/);
        else expect(params).toContain(status);
      }
    );

    it('answers 409 and sends no email when the build is not in a state the transition may start from', async () => {
      // A finished build: the guarded UPDATE matches no row.
      mockQuery.mockResolvedValueOnce(lookup('live'));
      mockQuery.mockResolvedValueOnce({ rowCount: 0, rows: [] });

      const res = await request(app)
        .post('/webhooks/build-status')
        .set('Authorization', 'Bearer test-webhook-secret')
        .send({ build_id: 'build-g', status: 'failed' });

      expect(res.status).toBe(409);
      expect(res.body.error).toMatch(/live/);
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(mockSendBuildFailedEmail).not.toHaveBeenCalled();
    });

    it('does not send the preview-ready mail when the preview write matched no row', async () => {
      mockQuery.mockResolvedValueOnce(lookup('live'));
      mockQuery.mockResolvedValueOnce({ rowCount: 0, rows: [] });

      const res = await request(app)
        .post('/webhooks/build-status')
        .set('Authorization', 'Bearer test-webhook-secret')
        .send({ build_id: 'build-g', status: 'preview', live_url: 'https://preview.up.railway.app' });

      expect(res.status).toBe(409);
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(mockSendPreviewReadyEmail).not.toHaveBeenCalled();
    });

    it('a preview URL is written to preview_url only, in the same guarded statement', async () => {
      mockQuery.mockResolvedValueOnce(lookup('deploying'));
      mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

      await request(app)
        .post('/webhooks/build-status')
        .set('Authorization', 'Bearer test-webhook-secret')
        .send({ build_id: 'build-g', status: 'preview', live_url: 'https://preview.up.railway.app' });

      const [sql, params] = updateCall();
      expect(sql).toContain('preview_url = COALESCE($1, preview_url)');
      expect(sql).not.toMatch(/live_url|completed_at/);
      expect(params[0]).toBe('https://preview.up.railway.app');
      expect(sql).toMatch(/status = ANY/);
    });

    it('arriving at preview puts the build into review: pending, with any earlier verdict cleared', async () => {
      mockQuery.mockResolvedValueOnce(lookup('deploying'));
      mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

      await request(app)
        .post('/webhooks/build-status')
        .set('Authorization', 'Bearer test-webhook-secret')
        .send({ build_id: 'build-g', status: 'preview' });

      const [sql, params] = updateCall();
      expect(sql).toMatch(/review_status = \$4/);
      expect(params[3]).toBe('pending');
      for (const column of ['reviewed_at', 'reviewed_by', 'review_notes', 'review_report', 'ai_reviewed_at']) {
        expect(sql).toMatch(new RegExp(`${column} = NULL`));
      }
      expect(sql).toMatch(/ai_review_iterations = 0/);
      // No URL in the report: the stored one is kept.
      expect(params[0]).toBeNull();
      expect(params[1]).toBe('build-g');
    });

    it.each([
      [undefined, 'pending'],
      ['', 'pending'],
      ['false', 'pending'],
      ['0', 'pending'],
      ['tru', 'pending'],
      ['true', 'ai_review_queued'],
      ['1', 'ai_review_queued'],
      ['yes', 'ai_review_queued'],
      ['on', 'ai_review_queued'],
      ['enabled', 'ai_review_queued'],
      [' TRUE ', 'ai_review_queued'],
    ])('with CAS_HARBOR_AUTO_REVIEW=%j a build arriving at preview is entered as %s, as the worker enters it', async (setting, expected) => {
      const saved = process.env.CAS_HARBOR_AUTO_REVIEW;
      if (setting === undefined) delete process.env.CAS_HARBOR_AUTO_REVIEW;
      else process.env.CAS_HARBOR_AUTO_REVIEW = setting;
      try {
        mockQuery.mockResolvedValueOnce(lookup('deploying'));
        mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });
        const res = await request(app)
          .post('/webhooks/build-status')
          .set('Authorization', 'Bearer test-webhook-secret')
          .send({ build_id: 'build-g', status: 'preview' });
        expect(res.status).toBe(200);
        expect(updateCall()[1][3]).toBe(expected);
      } finally {
        if (saved === undefined) delete process.env.CAS_HARBOR_AUTO_REVIEW;
        else process.env.CAS_HARBOR_AUTO_REVIEW = saved;
      }
    });

    it('the deployment address is read from preview_url, and from live_url, the name it was first given; preview_url wins when both are sent', async () => {
      const send = async (fields: Record<string, string>) => {
        mockQuery.mockReset();
        mockQuery.mockResolvedValueOnce(lookup('deploying'));
        mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });
        const res = await request(app)
          .post('/webhooks/build-status')
          .set('Authorization', 'Bearer test-webhook-secret')
          .send({ build_id: 'build-g', status: 'preview', ...fields });
        expect(res.status).toBe(200);
        return updateCall()[1][0];
      };
      expect(await send({ preview_url: 'https://new-name.example.test' })).toBe('https://new-name.example.test');
      expect(await send({ live_url: 'https://old-name.example.test' })).toBe('https://old-name.example.test');
      expect(await send({ preview_url: 'https://new-name.example.test', live_url: 'https://old-name.example.test' })).toBe('https://new-name.example.test');
      expect(mockSendPreviewReadyEmail).toHaveBeenLastCalledWith(expect.anything(), expect.anything(), expect.anything(), 'https://new-name.example.test', 'build-g', expect.anything());
    });

    it('an address that is not http(s) is refused under whichever name it came, and the error names that field', async () => {
      for (const field of ['preview_url', 'live_url']) {
        const res = await request(app)
          .post('/webhooks/build-status')
          .set('Authorization', 'Bearer test-webhook-secret')
          .send({ build_id: 'b-1', status: 'preview', [field]: 'javascript:alert(1)' });
        expect(res.status).toBe(400);
        expect(res.body.error).toBe(`${field} must be an http(s) URL.`);
      }
    });

    it.each(['building', 'testing', 'deploying', 'failed'])(
      '%s: a status that is not preview leaves the review columns alone',
      async (status) => {
        mockQuery.mockResolvedValueOnce(lookup('building'));
        mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });
        await request(app)
          .post('/webhooks/build-status')
          .set('Authorization', 'Bearer test-webhook-secret')
          .send({ build_id: 'build-g', status });
        const [sql] = updateCall();
        expect(sql).not.toMatch(/review_status|reviewed_at|review_notes/);
      }
    );
  });
});

// --- POST /webhooks/stripe ---
//
// The route runs each event in one transaction on a pooled client. The fake
// below is that client: every statement the route and its handlers issue is
// recorded in order and answered by the first rule whose pattern matches its
// SQL, so a test can assert the text and parameters of the statements that
// carry a guarantee (the claim, the lock, the guarded flip) instead of only
// counting calls. What these tests cannot show is that Postgres agrees: the
// statements are executed for real in money-path.realdb.test.ts.

const BUILD_ID = 'aaaaaaaa-1111-2222-3333-444444444444';
const TRIAL_ID = 'bbbbbbbb-1111-2222-3333-444444444444';
const CUSTOMER_ID = 'cccccccc-1111-2222-3333-444444444444';
const SESSION_ID = ['cs', 'test', 'harbor1'].join('_');
const STRIPE_CUSTOMER = ['cus', 'harbor1'].join('_');
const STRIPE_SUB = ['sub', 'harbor1'].join('_');
const NOW_SECONDS = Math.floor(Date.now() / 1000);

type Rows = { rows: unknown[]; rowCount?: number };
type Responder = Rows | Error | ((params: unknown[]) => Rows | Error);
interface Statement {
  sql: string;
  params: unknown[];
}

const db = {
  statements: [] as Statement[],
  rules: [] as Array<{ pattern: RegExp; respond: Responder }>,
  /** Everything observable, in order: statements and post-commit effects. */
  timeline: [] as string[],
  reset(): void {
    this.statements = [];
    this.rules = [];
    this.timeline = [];
  },
  /** Later rules win, so a test can override a default. */
  on(pattern: RegExp, respond: Responder): void {
    this.rules.unshift({ pattern, respond });
  },
  run(sql: unknown, params: unknown): Promise<Rows> {
    const text = String(sql);
    const values = Array.isArray(params) ? params : [];
    this.statements.push({ sql: text, params: values });
    this.timeline.push(text.trim().split(/\s+/).slice(0, 4).join(' '));
    const rule = this.rules.find((candidate) => candidate.pattern.test(text));
    const answer = rule
      ? typeof rule.respond === 'function'
        ? rule.respond(values)
        : rule.respond
      : { rows: [], rowCount: 0 };
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  },
  find(pattern: RegExp): Statement | undefined {
    return this.statements.find((statement) => pattern.test(statement.sql));
  },
  all(pattern: RegExp): Statement[] {
    return this.statements.filter((statement) => pattern.test(statement.sql));
  },
  indexOf(pattern: RegExp): number {
    return this.statements.findIndex((statement) => pattern.test(statement.sql));
  },
};

const CLAIM = /INSERT INTO processed_webhooks/;
const LOCK_BUILD = /FROM builds\s+WHERE id = \$1\s+FOR UPDATE/;
const FLIP_LIVE = /UPDATE builds\s+SET status = 'live'/;
const STAMP_PAID = /UPDATE builds\s+SET customer_id = COALESCE/;
const LOCK_TRIAL = /FROM trial_keepers\s+WHERE trial_id = \$1\s+FOR UPDATE/;
const STAMPED_TRIAL = /SELECT trial_id FROM trial_keepers\s+WHERE handoff_build_id = \$1 AND released_at IS NULL/;
const RELEASE_TRIAL = /UPDATE trial_keepers\s+SET released_at = NOW\(\)/;
/** The one statement that writes the keeper row (claimKeeperSubscription), whoever asks: a trial's promotion or a payment with no trial. */
const CLAIM_KEEPER = /INSERT INTO keepers[\s\S]*ON CONFLICT \(customer_id\) DO UPDATE[\s\S]*RETURNING customer_id/;
const RECORD_SUB = /INSERT INTO harbor_subscriptions[\s\S]*ON CONFLICT \(stripe_subscription_id\) DO NOTHING/;
const BLESSING = /INSERT INTO companion_turns[\s\S]*'harbor-blessing'/;
const ORDERING_COLUMN = /information_schema\.columns[\s\S]*last_event_created/;
const SUB_UPDATE = /UPDATE harbor_subscriptions/;
const SUB_LOOKUP = /SELECT status FROM harbor_subscriptions WHERE stripe_subscription_id = \$1/;
const TOMBSTONE_LOOKUP = /FROM harbor_subscription_tombstones WHERE stripe_subscription_id = \$1/;

function previewBuild(overrides: Record<string, unknown> = {}) {
  return {
    id: BUILD_ID,
    customer_id: CUSTOMER_ID,
    app_name: 'Lantern Studio',
    status: 'preview',
    review_status: 'approved',
    preview_url: 'https://preview.example.test',
    live_url: null,
    stripe_session_id: null,
    ...overrides,
  };
}

/** Rules for a trial visitor paying for an approved preview. */
function arrangeHappyPath(build = previewBuild()): void {
  db.on(CLAIM, (params) => ({ rows: [{ event_id: params[0] }] }));
  db.on(LOCK_BUILD, { rows: [build] });
  db.on(/SELECT email FROM customers WHERE id = \$1/, { rows: [{ email: 'payer@example.test' }] });
  db.on(LOCK_TRIAL, {
    rows: [{
      trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null,
      handoff_build_id: BUILD_ID, handoff_at: new Date('2026-04-25T09:00:00Z'),
    }],
  });
  db.on(CLAIM_KEEPER, { rows: [{ customer_id: CUSTOMER_ID }] });
  db.on(/UPDATE companion_turns/, { rows: [{ turn_id: 't1' }], rowCount: 1 });
  db.on(FLIP_LIVE, { rows: [{ id: BUILD_ID }] });
  db.on(BLESSING, { rows: [{ turn_id: 'blessing' }], rowCount: 1 });
}

function harborSession(overrides: Record<string, unknown> = {}, metadata: Record<string, unknown> = {}) {
  return {
    id: SESSION_ID,
    payment_status: 'paid',
    customer: STRIPE_CUSTOMER,
    subscription: STRIPE_SUB,
    customer_email: 'payer@example.test',
    metadata: {
      kind: 'harbor_make_public',
      build_id: BUILD_ID,
      trial_id: TRIAL_ID,
      monthly_amount_cents: '2900',
      sliding_scale_attestation: 'true',
      ...metadata,
    },
    ...overrides,
  };
}

let eventCounter = 0;
function stripeEvent(type: string, object: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  eventCounter += 1;
  return { id: `evt_w1_${eventCounter}`, type, created: NOW_SECONDS, data: { object }, ...extra };
}

async function deliver(event: unknown) {
  mockConstructWebhookEvent.mockReturnValueOnce(event);
  return request(app)
    .post('/webhooks/stripe')
    .set('stripe-signature', 'valid_sig')
    .set('Content-Type', 'application/json')
    .send(Buffer.from(JSON.stringify(event)));
}

function resetStripeMocks(): void {
  db.reset();
  mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
  mockClientQuery.mockReset().mockImplementation((sql: unknown, params: unknown) => db.run(sql, params));
  mockClientRelease.mockReset();
  mockPoolConnect.mockClear();
  mockConstructWebhookEvent.mockReset();
  mockSendHarborMadePublicEmail.mockReset().mockImplementation(() => {
    db.timeline.push('EMAIL made-public');
    return Promise.resolve();
  });
  mockSendPaymentNotAppliedEmail.mockReset().mockResolvedValue(undefined);
  resetEventOrderingCache();
  process.env.STRIPE_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET;
}

describe('POST /webhooks/stripe: request checks', () => {
  beforeEach(resetStripeMocks);

  it('returns 400 when the stripe-signature header is missing', async () => {
    const res = await request(app).post('/webhooks/stripe').set('Content-Type', 'application/json').send('{}');
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/signature/i);
    expect(mockPoolConnect).not.toHaveBeenCalled();
  });

  it('returns 500 when STRIPE_WEBHOOK_SECRET is not configured', async () => {
    delete process.env.STRIPE_WEBHOOK_SECRET;
    const res = await request(app)
      .post('/webhooks/stripe')
      .set('stripe-signature', 'sig')
      .set('Content-Type', 'application/json')
      .send('{}');
    expect(res.status).toBe(500);
    expect(res.body.error).toMatch(/not configured/i);
  });

  it('returns 400 when signature verification fails, without touching the database', async () => {
    mockConstructWebhookEvent.mockImplementationOnce(() => {
      throw new Error('Signature mismatch');
    });
    const res = await request(app)
      .post('/webhooks/stripe')
      .set('stripe-signature', 'bad')
      .set('Content-Type', 'application/json')
      .send('{}');
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/invalid signature/i);
    expect(mockPoolConnect).not.toHaveBeenCalled();
  });

  it('refuses an event that has no id (it could not be claimed, so it could be applied twice)', async () => {
    const res = await deliver({ type: 'checkout.session.completed', data: { object: harborSession() } });
    expect(res.status).toBe(400);
    expect(mockPoolConnect).not.toHaveBeenCalled();
  });

  it('answers 500 when no database connection can be had', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockPoolConnect.mockRejectedValueOnce(new Error('pool exhausted'));
    const res = await deliver(stripeEvent('customer.created', {}));
    expect(res.status).toBe(500);
    errSpy.mockRestore();
  });
});

describe('POST /webhooks/stripe: the claim and the work are one transaction', () => {
  beforeEach(resetStripeMocks);
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('claims inside the transaction, as its first statement, and commits last', async () => {
    arrangeHappyPath();
    const event = stripeEvent('checkout.session.completed', harborSession());
    const res = await deliver(event);

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ received: true });
    const sqls = db.statements.map((statement) => statement.sql);
    expect(sqls[0]).toBe('BEGIN');
    expect(sqls[1]).toMatch(CLAIM);
    expect(sqls[1]).toMatch(/ON CONFLICT \(event_id\) DO NOTHING RETURNING event_id/);
    expect(db.statements[1].params).toEqual([event.id, 'checkout.session.completed']);
    expect(sqls[sqls.length - 1]).toBe('COMMIT');
    expect(sqls.filter((sql) => sql === 'COMMIT')).toHaveLength(1);
    expect(sqls).not.toContain('ROLLBACK');
    expect(mockClientRelease).toHaveBeenCalledOnce();
  });

  it('runs every statement of the event on the one transaction client, none on the pool', async () => {
    arrangeHappyPath();
    await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(mockPoolConnect).toHaveBeenCalledOnce();
    // The only thing allowed on the pool is the hourly prune of old claims.
    for (const [sql] of mockQuery.mock.calls) {
      expect(String(sql)).toMatch(/DELETE FROM processed_webhooks WHERE processed_at </);
    }
    expect(db.indexOf(FLIP_LIVE)).toBeGreaterThan(db.indexOf(CLAIM));
    expect(db.indexOf(RELEASE_TRIAL)).toBeGreaterThan(db.indexOf(CLAIM));
  });

  it('a duplicate delivery (claim already committed) is acknowledged and does nothing', async () => {
    arrangeHappyPath();
    db.on(CLAIM, { rows: [] }); // ON CONFLICT DO NOTHING returned no row
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ received: true, duplicate: true });
    expect(db.statements.map((statement) => statement.sql)).toEqual(['BEGIN', expect.stringMatching(CLAIM), 'ROLLBACK']);
    expect(mockSendHarborMadePublicEmail).not.toHaveBeenCalled();
    expect(mockClientRelease).toHaveBeenCalledOnce();
  });

  it('a handler failure rolls everything back, claim included, and answers 500 so Stripe retries', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(BLESSING, new Error('connection reset'));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(500);
    const sqls = db.statements.map((statement) => statement.sql);
    expect(sqls[sqls.length - 1]).toBe('ROLLBACK');
    expect(sqls).not.toContain('COMMIT');
    // The claim is given back by the rollback itself: no separate DELETE that could fail on its own.
    expect(db.find(/DELETE FROM processed_webhooks/)).toBeUndefined();
    expect(mockQuery.mock.calls.some(([sql]) => /DELETE FROM processed_webhooks WHERE event_id/.test(String(sql)))).toBe(false);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendHarborMadePublicEmail).not.toHaveBeenCalled();
    expect(mockClientRelease).toHaveBeenCalledWith(undefined);
    expect(errSpy).toHaveBeenCalledWith('Webhook processing error:', 'connection reset');
  });

  it('a failed COMMIT answers 500 and sends nothing', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(/^COMMIT$/, new Error('commit failed'));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(500);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendHarborMadePublicEmail).not.toHaveBeenCalled();
    expect(db.statements[db.statements.length - 1].sql).toBe('ROLLBACK');
  });

  it('a connection whose ROLLBACK failed is destroyed, not returned to the pool', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(FLIP_LIVE, new Error('connection terminated'));
    db.on(/^ROLLBACK$/, new Error('connection terminated'));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(500);
    expect(mockClientRelease).toHaveBeenCalledWith(true);
  });

  it('the email goes out only after COMMIT', async () => {
    arrangeHappyPath();
    await deliver(stripeEvent('checkout.session.completed', harborSession()));
    await vi.waitFor(() => expect(mockSendHarborMadePublicEmail).toHaveBeenCalledOnce());

    expect(db.timeline.indexOf('EMAIL made-public')).toBeGreaterThan(db.timeline.indexOf('COMMIT'));
    expect(mockSendHarborMadePublicEmail.mock.calls[0][0]).toMatchObject({
      to: 'payer@example.test',
      appName: 'Lantern Studio',
      publicUrl: 'https://preview.example.test',
      buildId: BUILD_ID,
      monthlyAmountCents: 2900,
    });
  });

  it('claims and commits an event type it does not handle', async () => {
    db.on(CLAIM, (params) => ({ rows: [{ event_id: params[0] }] }));
    const res = await deliver(stripeEvent('customer.created', {}));
    expect(res.status).toBe(200);
    expect(db.statements.map((statement) => statement.sql)).toEqual(['BEGIN', expect.stringMatching(CLAIM), 'COMMIT']);
  });

  it('ignores a paid checkout that is not a Harbor checkout (nothing in this app creates one)', async () => {
    db.on(CLAIM, (params) => ({ rows: [{ event_id: params[0] }] }));
    const res = await deliver(
      stripeEvent('checkout.session.completed', { id: 'cs_other', payment_status: 'paid', metadata: { build_id: BUILD_ID, tier: 'build' } })
    );
    expect(res.status).toBe(200);
    expect(db.find(/builds/)).toBeUndefined();
    expect(mockSendBuildQueuedEmail).not.toHaveBeenCalled();
  });

  it('payment_intent.payment_failed changes no build (no build is ever waiting on a payment intent)', async () => {
    db.on(CLAIM, (params) => ({ rows: [{ event_id: params[0] }] }));
    const res = await deliver(stripeEvent('payment_intent.payment_failed', { id: 'pi_1', last_payment_error: { message: 'declined' } }));
    expect(res.status).toBe(200);
    expect(db.find(/UPDATE builds/)).toBeUndefined();
  });
});

describe('POST /webhooks/stripe: harbor_make_public takes a reviewed preview live', () => {
  beforeEach(resetStripeMocks);
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('locks the build row before deciding anything about it', async () => {
    arrangeHappyPath();
    await deliver(stripeEvent('checkout.session.completed', harborSession()));

    const lock = db.find(LOCK_BUILD);
    expect(lock).toBeDefined();
    expect(lock!.params).toEqual([BUILD_ID]);
    // Nothing reads or writes builds before the lock.
    expect(db.indexOf(/builds/)).toBe(db.indexOf(LOCK_BUILD));
  });

  it('the status change carries its own gate: preview AND approved, in the UPDATE itself', async () => {
    arrangeHappyPath();
    await deliver(stripeEvent('checkout.session.completed', harborSession()));

    const flip = db.find(FLIP_LIVE);
    expect(flip).toBeDefined();
    expect(flip!.sql).toMatch(/WHERE id = \$1 AND status = 'preview' AND review_status = 'approved'/);
    expect(flip!.sql).toMatch(/live_url = COALESCE\(live_url, preview_url\)/);
    expect(flip!.sql).toMatch(/customer_id = COALESCE\(customer_id, \$2\)/);
    expect(flip!.sql).toMatch(/stripe_session_id = \$3/);
    expect(flip!.params).toEqual([BUILD_ID, CUSTOMER_ID, SESSION_ID]);
    // It is the only statement in the event that writes a status.
    expect(db.all(/UPDATE builds/)).toHaveLength(1);
  });

  it('promotes the trial into the build\'s customer in the same transaction', async () => {
    arrangeHappyPath();
    await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(db.find(LOCK_TRIAL)!.params).toEqual([TRIAL_ID]);
    const moved = db.find(/UPDATE companion_turns\s+SET keeper_id = \$1, trial_id = NULL/);
    expect(moved!.params).toEqual([CUSTOMER_ID, TRIAL_ID]);
    // Promotion wrote the keeper row, once, with the claim that replaces nothing; the keeper-path claim is not made as well.
    const claims = db.all(CLAIM_KEEPER);
    expect(claims).toHaveLength(1);
    expect(claims[0].params[10]).toBe(false);
    // The release records the account the trial went to.
    expect(db.find(RELEASE_TRIAL)!.sql).toMatch(/promoted_customer_id = \$2/);
    expect(db.find(RELEASE_TRIAL)!.params).toEqual([TRIAL_ID, CUSTOMER_ID]);
    // And it all happened before COMMIT.
    expect(db.indexOf(RELEASE_TRIAL)).toBeLessThan(db.indexOf(/^COMMIT$/));
  });

  it('records the subscription with the amount from the metadata and writes the blessing turn', async () => {
    arrangeHappyPath();
    await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(db.find(RECORD_SUB)!.params).toEqual([CUSTOMER_ID, STRIPE_SUB, STRIPE_CUSTOMER, 2900]);
    const blessing = db.find(BLESSING);
    expect(blessing!.params[0]).toBe(CUSTOMER_ID);
    expect(String(blessing!.params[1]).length).toBeGreaterThan(20);
  });

  it('if the locked, publishable build somehow does not flip, the whole event fails rather than commit a promotion with nothing live', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(FLIP_LIVE, { rows: [] });
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(500);
    expect(db.statements.map((statement) => statement.sql)).not.toContain('COMMIT');
    expect(db.statements[db.statements.length - 1].sql).toBe('ROLLBACK');
  });

  it.each([
    ['still waiting for review', { review_status: 'pending' }],
    ['changes requested', { review_status: 'changes_requested' }],
    ['requeued', { status: 'queued', review_status: null }],
    ['failed', { status: 'failed', review_status: 'approved' }],
  ])('a paid session for a build that is %s: no promotion, no status change, the charge is recorded', async (_label, state) => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath(previewBuild(state));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.find(FLIP_LIVE)).toBeUndefined();
    expect(db.find(STAMP_PAID)).toBeUndefined();
    expect(db.find(LOCK_TRIAL)).toBeUndefined();
    expect(db.find(RELEASE_TRIAL)).toBeUndefined();
    expect(db.find(BLESSING)).toBeUndefined();
    expect(db.find(RECORD_SUB)!.params).toEqual([CUSTOMER_ID, STRIPE_SUB, STRIPE_CUSTOMER, 2900]);
    expect(errSpy.mock.calls.some(([line]) => /PAID session .* cannot take build/.test(String(line)))).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendHarborMadePublicEmail).not.toHaveBeenCalled();
    // The operator is told, after the commit, with the ids and without the customer's address.
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
    expect(db.timeline.indexOf('COMMIT')).toBeGreaterThan(-1);
    expect(mockSendPaymentNotAppliedEmail.mock.calls[0][0]).toMatchObject({
      sessionId: SESSION_ID,
      stripeSubscriptionId: STRIPE_SUB,
      buildId: BUILD_ID,
      customerId: CUSTOMER_ID,
      trialId: TRIAL_ID,
      monthlyAmountCents: 2900,
    });
  });

  it('a second delivery for the session that already took the build live writes nothing', async () => {
    arrangeHappyPath(previewBuild({ status: 'live', live_url: 'https://preview.example.test', stripe_session_id: SESSION_ID }));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.all(/^\s*(INSERT|UPDATE)/).filter((statement) => !CLAIM.test(statement.sql))).toHaveLength(0);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendHarborMadePublicEmail).not.toHaveBeenCalled();
  });

  it('a different paid session for a build that is already live and paid for is recorded, not applied', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath(previewBuild({ status: 'live', stripe_session_id: 'cs_first_payment' }));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.find(RECORD_SUB)!.params).toEqual([CUSTOMER_ID, STRIPE_SUB, STRIPE_CUSTOMER, 2900]);
    expect(db.find(/UPDATE builds/)).toBeUndefined();
    expect(db.find(RELEASE_TRIAL)).toBeUndefined();
    expect(errSpy.mock.calls.some(([line]) => /paid_by=cs_first_payment/.test(String(line)))).toBe(true);
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
  });

  it('a build an admin already took live, never paid for: the payer is promoted and recorded, the status is not touched', async () => {
    arrangeHappyPath(previewBuild({ status: 'live', live_url: 'https://preview.example.test', stripe_session_id: null }));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.find(FLIP_LIVE)).toBeUndefined();
    const stamp = db.find(STAMP_PAID);
    expect(stamp!.sql).toMatch(/WHERE id = \$1 AND status = 'live' AND stripe_session_id IS NULL/);
    expect(stamp!.params).toEqual([BUILD_ID, CUSTOMER_ID, SESSION_ID]);
    expect(db.find(RELEASE_TRIAL)).toBeDefined();
    await vi.waitFor(() => expect(mockSendHarborMadePublicEmail).toHaveBeenCalledOnce());
  });

  it.each(['unpaid', 'no_payment_required'])('payment_status=%s never reaches the build', async (paymentStatus) => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({ payment_status: paymentStatus })));

    expect(res.status).toBe(200);
    expect(db.find(/builds/)).toBeUndefined();
    expect(db.find(/trial_keepers/)).toBeUndefined();
  });

  it.each([
    ['a missing build_id', { build_id: '' }],
    ['a build_id that is not a UUID', { build_id: "x' OR 1=1" }],
    ['a non-positive amount', { monthly_amount_cents: '0' }],
  ])('%s is logged and acknowledged without a query', async (_label, metadata) => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({}, metadata)));

    expect(res.status).toBe(200);
    expect(db.statements.map((statement) => statement.sql)).toEqual(['BEGIN', expect.stringMatching(CLAIM), 'COMMIT']);
  });

  it('customer or subscription ids of the wrong shape are refused before the build is read', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({ subscription: 'not-a-subscription' })));
    expect(res.status).toBe(200);
    expect(db.find(LOCK_BUILD)).toBeUndefined();
    // The session is paid, so the operator is told: the claim is committed and Stripe will not redeliver.
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
    expect(mockSendPaymentNotAppliedEmail.mock.calls[0][0]).toMatchObject({
      sessionId: SESSION_ID,
      buildId: BUILD_ID,
      customerId: null,
      reason: expect.stringMatching(/customer or subscription id/),
    });
  });

  describe('a paid session whose metadata cannot be acted on is acknowledged and reported, never dropped silently', () => {
    const cases: Array<[string, Record<string, unknown>, Record<string, unknown>, RegExp]> = [
      ['a build_id that is not a UUID', {}, { build_id: 'not-a-uuid' }, /build_id/],
      ['no build_id at all', {}, { build_id: undefined }, /build_id/],
      ['a monthly amount that is not a positive whole number of cents', {}, { monthly_amount_cents: 'abc' }, /monthly_amount_cents/],
      ['a session with no id', { id: undefined }, {}, /no id/],
    ];

    it.each(cases)('%s: no build is read or written, and the operator is told', async (_label, sessionOverrides, metadata, reason) => {
      vi.spyOn(console, 'error').mockImplementation(() => {});
      arrangeHappyPath();
      const res = await deliver(stripeEvent('checkout.session.completed', harborSession(sessionOverrides, metadata)));

      expect(res.status).toBe(200);
      expect(db.find(LOCK_BUILD)).toBeUndefined();
      expect(db.all(/^\s*(INSERT|UPDATE)/).filter((statement) => !CLAIM.test(statement.sql))).toHaveLength(0);
      await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
      expect(mockSendPaymentNotAppliedEmail.mock.calls[0][0]).toMatchObject({
        customerId: null,
        stripeSubscriptionId: STRIPE_SUB,
        reason: expect.stringMatching(reason),
      });
    });

    it('an amount that cannot be read is not reported as an amount', async () => {
      vi.spyOn(console, 'error').mockImplementation(() => {});
      arrangeHappyPath();
      await deliver(stripeEvent('checkout.session.completed', harborSession({}, { monthly_amount_cents: '-5' })));
      await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
      expect(mockSendPaymentNotAppliedEmail.mock.calls[0][0].monthlyAmountCents).toBe(0);
    });

    it('an unpaid session with the same bad metadata took no money: logged, acknowledged, no alert', async () => {
      const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      arrangeHappyPath();
      const res = await deliver(
        stripeEvent('checkout.session.completed', harborSession({ payment_status: 'unpaid' }, { build_id: 'not-a-uuid' }))
      );

      expect(res.status).toBe(200);
      expect(db.find(LOCK_BUILD)).toBeUndefined();
      expect(errSpy.mock.calls.some(([line]) => /build_id/.test(String(line)))).toBe(true);
      // Give any after-commit step the chance to run before asserting it did not.
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(mockSendPaymentNotAppliedEmail).not.toHaveBeenCalled();
    });
  });

  it('a paid session for a build that does not exist is logged and acknowledged', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(LOCK_BUILD, { rows: [] });
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.all(/^\s*(INSERT|UPDATE)/).filter((statement) => !CLAIM.test(statement.sql))).toHaveLength(0);
    expect(errSpy.mock.calls.some(([line]) => /does not exist/.test(String(line)))).toBe(true);
    // Nothing could be recorded, so the operator is the only record: the alert carries no account.
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
    expect(mockSendPaymentNotAppliedEmail.mock.calls[0][0]).toMatchObject({ buildId: BUILD_ID, customerId: null });
  });

  it('a build with no customer gets one created for the checkout email: unverified, with a bcrypt hash nobody knows', async () => {
    arrangeHappyPath(previewBuild({ customer_id: null }));
    db.on(/INSERT INTO customers/, { rows: [{ id: 'new-customer-id' }] });
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({ customer_email: '  New.Payer@Example.Test ' })));

    expect(res.status).toBe(200);
    const insert = db.find(/INSERT INTO customers/);
    expect(insert!.sql).toMatch(/\(email, password, email_verified, name\)\s+VALUES \(\$1, \$2, FALSE, \$3\)\s+ON CONFLICT \(email\) DO NOTHING/);
    expect(insert!.params[0]).toBe('new.payer@example.test');
    expect(String(insert!.params[1])).toMatch(/^\$2[aby]\$12\$/);
    expect(insert!.params[2]).toBe('new.payer');
    expect(db.find(FLIP_LIVE)!.params).toEqual([BUILD_ID, 'new-customer-id', SESSION_ID]);
  });

  it('a build with no customer and a session with no email cannot be attached to anyone: nothing is written', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath(previewBuild({ customer_id: null }));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({ customer_email: undefined })));

    expect(res.status).toBe(200);
    expect(db.find(FLIP_LIVE)).toBeUndefined();
    expect(db.find(/INSERT INTO customers/)).toBeUndefined();
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
  });

  it('a payer who is already a keeper (no trial in the metadata): the keeper row is upserted before the blessing, then the build flips', async () => {
    arrangeHappyPath();
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({}, { trial_id: '' })));

    expect(res.status).toBe(200);
    expect(db.find(LOCK_TRIAL)).toBeUndefined();
    const claim = db.find(CLAIM_KEEPER);
    // No trial: nothing to carry over, and a subscription that no longer publishes may be replaced.
    expect(claim!.params.slice(0, 11)).toEqual([CUSTOMER_ID, 'Haven', 'Haven', 'active', STRIPE_SUB, STRIPE_CUSTOMER, true, 2900, null, null, true]);
    expect(claim!.params[11]).toEqual([...PUBLISHING_SUBSCRIPTION_STATUSES]);
    expect(db.indexOf(CLAIM_KEEPER)).toBeLessThan(db.indexOf(BLESSING));
    expect(db.find(FLIP_LIVE)).toBeDefined();
  });

  it('a trial that no longer exists does not strand the payment: the keeper row is made directly and the build goes live', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(LOCK_TRIAL, { rows: [] });
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.find(RELEASE_TRIAL)).toBeUndefined();
    expect(db.find(CLAIM_KEEPER)!.params[10]).toBe(true);
    expect(db.find(FLIP_LIVE)).toBeDefined();
  });

  it('a trial id that is not a UUID is never sent to the database; the build is asked for its trial instead', async () => {
    arrangeHappyPath();
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({}, { trial_id: 'not-a-uuid' })));
    expect(res.status).toBe(200);
    expect(db.statements.some((statement) => statement.params.includes('not-a-uuid'))).toBe(false);
    // The only trial_keepers statement is the lookup by build, which finds none here.
    const asked = db.all(/trial_keepers/);
    expect(asked).toHaveLength(1);
    expect(asked[0].sql).toMatch(STAMPED_TRIAL);
    expect(asked[0].params).toEqual([BUILD_ID]);
    expect(db.find(CLAIM_KEEPER)).toBeDefined();
  });

  it('a session that names no trial, for a build an unreleased trial is stamped with: that trial is promoted, so the conversation moves with the payment', async () => {
    // What an owner who logged in before paying sends when the Checkout
    // session was created without the trial: the site must not go live with
    // the conversation left behind on the trial.
    arrangeHappyPath();
    db.on(STAMPED_TRIAL, (params) => ({ rows: params[0] === BUILD_ID ? [{ trial_id: TRIAL_ID }] : [] }));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({}, { trial_id: '' })));

    expect(res.status).toBe(200);
    expect(db.find(LOCK_TRIAL)!.params).toEqual([TRIAL_ID]);
    const moved = db.find(/UPDATE companion_turns\s+SET keeper_id = \$1, trial_id = NULL/);
    expect(moved!.params).toEqual([CUSTOMER_ID, TRIAL_ID]);
    expect(db.find(RELEASE_TRIAL)).toBeDefined();
    // Promotion wrote the keeper row; the claim for "already a keeper" was not made as well.
    expect(db.all(CLAIM_KEEPER)).toHaveLength(1);
    // The lookup comes after the build is locked and before the trial is.
    expect(db.indexOf(LOCK_BUILD)).toBeLessThan(db.indexOf(STAMPED_TRIAL));
    expect(db.indexOf(STAMPED_TRIAL)).toBeLessThan(db.indexOf(LOCK_TRIAL));
    expect(db.find(FLIP_LIVE)).toBeDefined();
  });

  it('a session that names a trial is not second-guessed: the build is not asked', async () => {
    arrangeHappyPath();
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));
    expect(res.status).toBe(200);
    expect(db.find(STAMPED_TRIAL)).toBeUndefined();
  });

  it('a keeper that has a subscription of its own refuses the trial: nothing is promoted, the build stays in preview, the payment is recorded and the operator told', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(CLAIM_KEEPER, { rows: [] }); // the claim matched no row: the keeper belongs to another subscription
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.find(FLIP_LIVE)).toBeUndefined();
    expect(db.find(STAMP_PAID)).toBeUndefined();
    expect(db.find(RELEASE_TRIAL)).toBeUndefined();
    expect(db.find(/UPDATE companion_turns/)).toBeUndefined();
    expect(db.find(BLESSING)).toBeUndefined();
    // It did not fall through to the keeper-path claim either: one claim, the trial's.
    expect(db.all(CLAIM_KEEPER)).toHaveLength(1);
    expect(db.find(RECORD_SUB)!.params).toEqual([CUSTOMER_ID, STRIPE_SUB, STRIPE_CUSTOMER, 2900]);
    expect(db.statements[db.statements.length - 1].sql).toBe('COMMIT');
    expect(errSpy.mock.calls.some(([line]) => /PAID session .* was not applied/.test(String(line)))).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(mockSendHarborMadePublicEmail).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
    expect(mockSendPaymentNotAppliedEmail.mock.calls[0][0].reason).toMatch(/already has a keeper with a subscription/);
  });

  it('a payment with no trial that the keeper-path claim refuses is recorded and flagged the same way, and the build stays in preview', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(CLAIM_KEEPER, { rows: [] });
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession({}, { trial_id: '' })));

    expect(res.status).toBe(200);
    expect(db.find(LOCK_TRIAL)).toBeUndefined();
    expect(db.find(FLIP_LIVE)).toBeUndefined();
    expect(db.find(BLESSING)).toBeUndefined();
    expect(db.find(RECORD_SUB)).toBeDefined();
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
  });

  it('a trial that is not the one stamped on the paid build is refused: nothing is promoted, the payment is recorded and the operator told', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(LOCK_TRIAL, {
      rows: [{
        trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null,
        handoff_build_id: 'dddddddd-1111-2222-3333-444444444444', handoff_at: new Date('2026-04-25T09:00:00Z'),
      }],
    });
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.find(CLAIM_KEEPER)).toBeUndefined();
    expect(db.find(RELEASE_TRIAL)).toBeUndefined();
    expect(db.find(FLIP_LIVE)).toBeUndefined();
    expect(db.find(RECORD_SUB)).toBeDefined();
    await vi.waitFor(() => expect(mockSendPaymentNotAppliedEmail).toHaveBeenCalledOnce());
    expect(mockSendPaymentNotAppliedEmail.mock.calls[0][0].reason).toMatch(/not the one stamped on its build/);
  });

  it('a mail failure is logged and never fails the event: it is committed and acknowledged', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    arrangeHappyPath();
    db.on(CLAIM_KEEPER, { rows: [] });
    mockSendPaymentNotAppliedEmail.mockRejectedValueOnce(new Error('mail down'));
    const res = await deliver(stripeEvent('checkout.session.completed', harborSession()));

    expect(res.status).toBe(200);
    expect(db.statements[db.statements.length - 1].sql).toBe('COMMIT');
    await vi.waitFor(() =>
      expect(errSpy.mock.calls.some(([line]) => /sendPaymentNotAppliedEmail failed/.test(String(line)))).toBe(true)
    );
  });
});

describe('POST /webhooks/stripe: subscription lifecycle events', () => {
  beforeEach(() => {
    resetStripeMocks();
    db.on(CLAIM, (params) => ({ rows: [{ event_id: params[0] }] }));
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const subscription = (overrides: Record<string, unknown> = {}) => ({
    id: STRIPE_SUB,
    status: 'past_due',
    cancel_at_period_end: true,
    current_period_start: 1_760_000_000,
    current_period_end: 1_762_592_000,
    metadata: { kind: 'harbor_make_public', build_id: BUILD_ID },
    ...overrides,
  });

  function withOrderingColumn(present: boolean): void {
    db.on(ORDERING_COLUMN, { rows: present ? [{ '?column?': 1 }] : [] });
  }

  it('subscription.updated: one statement updates the audit row and its keeper mirror', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 1, mirrored: 1 }] });
    const res = await deliver(stripeEvent('customer.subscription.updated', subscription(), { created: 1_761_000_000 }));

    expect(res.status).toBe(200);
    const updates = db.all(SUB_UPDATE);
    expect(updates).toHaveLength(1);
    const { sql, params } = updates[0];
    // Rule 1: nothing leaves `canceled`.
    expect(sql).toMatch(/WHERE stripe_subscription_id = \$5\s+AND status <> 'canceled'/);
    // Rule 2: an event older than what the row reflects is skipped.
    expect(sql).toMatch(/last_event_created = \$7,/);
    expect(sql).toMatch(/AND \(last_event_created IS NULL OR last_event_created <= \$7\)/);
    // Rule 3: the keeper row is matched by this subscription, not by customer alone.
    expect(sql).toMatch(/UPDATE keepers k[\s\S]*WHERE k\.customer_id = applied\.keeper_customer_id\s+AND k\.stripe_subscription_id = \$5/);
    expect(params).toEqual([
      'past_due',
      true,
      new Date(1_760_000_000 * 1000),
      new Date(1_762_592_000 * 1000),
      STRIPE_SUB,
      'past_due',
      1_761_000_000,
    ]);
    // No second, separate write to keepers.
    expect(db.all(/^\s*UPDATE keepers/)).toHaveLength(0);
  });

  // This test used to pin `unpaid` being folded into `past_due` on the keeper
  // row ("the nearest value the column allows"). past_due publishes, so that
  // let a subscription Stripe had given up on keep publishing. The keeper
  // column now takes Stripe's word as it is.
  it.each(STRIPE_SUBSCRIPTION_STATUSES)(
    'subscription.updated writes Stripe status %s to the audit row and to the keeper row unchanged',
    async (status) => {
      withOrderingColumn(true);
      db.on(SUB_UPDATE, { rows: [{ applied: 1, mirrored: 1 }] });
      await deliver(stripeEvent('customer.subscription.updated', subscription({ status })));
      const { params } = db.find(SUB_UPDATE)!;
      expect(params[0]).toBe(status);
      expect(params[5]).toBe(status);
    }
  );

  it('a status this code does not know is kept on the audit row and becomes incomplete, which never publishes, on the keeper row', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 1, mirrored: 1 }] });
    await deliver(stripeEvent('customer.subscription.updated', subscription({ status: 'some_future_state' })));
    const { params } = db.find(SUB_UPDATE)!;
    expect(params[0]).toBe('some_future_state');
    expect(params[5]).toBe('incomplete');
    expect(PUBLISHING_SUBSCRIPTION_STATUSES).not.toContain('incomplete');
  });

  it('the keeper value never carries the right to publish unless Stripe\'s own status does', () => {
    expect([...PUBLISHING_SUBSCRIPTION_STATUSES].sort()).toEqual(['active', 'past_due', 'trialing']);
    for (const status of [...STRIPE_SUBSCRIPTION_STATUSES, 'some_future_state', '', 'ACTIVE']) {
      const stored = keeperSubscriptionStatus(status);
      expect(PUBLISHING_SUBSCRIPTION_STATUSES.includes(stored), status).toBe(
        PUBLISHING_SUBSCRIPTION_STATUSES.includes(status)
      );
    }
    // The states the finding was about, by name.
    for (const status of ['unpaid', 'incomplete_expired', 'paused', 'incomplete', 'canceled']) {
      expect(PUBLISHING_SUBSCRIPTION_STATUSES).not.toContain(keeperSubscriptionStatus(status));
    }
  });

  it('an event for a subscription that went away with its deleted account is acknowledged, not refused for a day', async () => {
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 0, mirrored: 0 }] });
    db.on(SUB_LOOKUP, { rows: [] });
    db.on(TOMBSTONE_LOOKUP, { rows: [{ '?column?': 1 }] });
    for (const type of ['customer.subscription.updated', 'customer.subscription.deleted']) {
      const res = await deliver(stripeEvent(type, subscription(), { created: NOW_SECONDS - 60 }));
      expect(res.status, type).toBe(200);
      expect(db.statements[db.statements.length - 1].sql).toBe('COMMIT');
    }
    expect(db.find(TOMBSTONE_LOOKUP)!.params).toEqual([STRIPE_SUB]);
    expect(logSpy.mock.calls.some(([line]) => /its account was deleted/.test(String(line)))).toBe(true);
  });

  it('without the ordering column the terminal-state rule still applies, and the gap is logged', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    withOrderingColumn(false);
    db.on(SUB_UPDATE, { rows: [{ applied: 1, mirrored: 1 }] });
    const res = await deliver(stripeEvent('customer.subscription.updated', subscription()));

    expect(res.status).toBe(200);
    const { sql, params } = db.find(SUB_UPDATE)!;
    expect(sql).toMatch(/AND status <> 'canceled'/);
    expect(sql).not.toMatch(/last_event_created/);
    expect(params).toHaveLength(6);
    expect(warnSpy.mock.calls.some(([line]) => /last_event_created is missing/.test(String(line)))).toBe(true);
  });

  it('asks about the ordering column once per process once it is there', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 1, mirrored: 1 }] });
    await deliver(stripeEvent('customer.subscription.updated', subscription()));
    await deliver(stripeEvent('customer.subscription.updated', subscription()));
    expect(db.all(ORDERING_COLUMN)).toHaveLength(1);
  });

  it('a non-Harbor subscription is acknowledged untouched', async () => {
    const res = await deliver(stripeEvent('customer.subscription.updated', subscription({ metadata: {} })));
    expect(res.status).toBe(200);
    expect(db.find(SUB_UPDATE)).toBeUndefined();
  });

  it('an event that overtook its checkout (no row yet) is refused with 500 and not claimed, so Stripe redelivers it', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 0, mirrored: 0 }] });
    db.on(SUB_LOOKUP, { rows: [] });
    const res = await deliver(stripeEvent('customer.subscription.updated', subscription(), { created: NOW_SECONDS - 60 }));

    expect(res.status).toBe(500);
    const sqls = db.statements.map((statement) => statement.sql);
    expect(sqls[sqls.length - 1]).toBe('ROLLBACK');
    expect(sqls).not.toContain('COMMIT');
  });

  it('an event for a subscription still unknown 30 hours later is refused with 500, so Stripe retries it', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 0, mirrored: 0 }] });
    db.on(SUB_LOOKUP, { rows: [] });
    const res = await deliver(
      stripeEvent('customer.subscription.updated', subscription(), { created: NOW_SECONDS - 30 * 60 * 60 })
    );

    expect(res.status).toBe(500);
    const sqls = db.statements.map((statement) => statement.sql);
    expect(sqls[sqls.length - 1]).toBe('ROLLBACK');
    expect(sqls).not.toContain('COMMIT');
  });

  it('an event for a subscription still unknown 80 hours later (past the 72-hour retry window) is acknowledged and dropped, with an error line', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 0, mirrored: 0 }] });
    db.on(SUB_LOOKUP, { rows: [] });
    const res = await deliver(
      stripeEvent('customer.subscription.updated', subscription(), { created: NOW_SECONDS - 80 * 60 * 60 })
    );

    expect(res.status).toBe(200);
    expect(db.statements[db.statements.length - 1].sql).toBe('COMMIT');
    expect(errSpy.mock.calls.some(([line]) => /unknown subscription .* dropped/.test(String(line)))).toBe(true);
  });

  it('an event the rules skipped (row exists, canceled or newer) is acknowledged without a retry', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 0, mirrored: 0 }] });
    db.on(SUB_LOOKUP, { rows: [{ status: 'canceled' }] });
    const res = await deliver(stripeEvent('customer.subscription.updated', subscription({ status: 'active' })));

    expect(res.status).toBe(200);
    expect(db.statements[db.statements.length - 1].sql).toBe('COMMIT');
  });

  it('subscription.deleted: canceled in both tables in one statement, whatever order it arrives in', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 1, mirrored: 1 }] });
    const res = await deliver(stripeEvent('customer.subscription.deleted', subscription(), { created: 1_761_500_000 }));

    expect(res.status).toBe(200);
    const { sql, params } = db.find(SUB_UPDATE)!;
    expect(sql).toMatch(/SET status = 'canceled',\s+cancel_at_period_end = FALSE,/);
    expect(sql).toMatch(/last_event_created = GREATEST\(COALESCE\(last_event_created, 0\), \$2\)/);
    // No ordering or status predicate: `deleted` always applies.
    expect(sql).toMatch(/WHERE stripe_subscription_id = \$1\s+RETURNING keeper_customer_id/);
    expect(sql).toMatch(/SET subscription_status = 'canceled'[\s\S]*AND k\.stripe_subscription_id = \$1/);
    expect(params).toEqual([STRIPE_SUB, 1_761_500_000]);
    // The build is not touched: the website stays up.
    expect(db.find(/builds/)).toBeUndefined();
  });

  it('subscription.deleted for a subscription with no row yet is refused so it is retried', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 0, mirrored: 0 }] });
    db.on(SUB_LOOKUP, { rows: [] });
    const res = await deliver(stripeEvent('customer.subscription.deleted', subscription()));
    expect(res.status).toBe(500);
  });

  it('invoice.payment_failed: past_due in both tables in one statement, never over canceled, never out of order', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 1, mirrored: 1 }] });
    const res = await deliver(stripeEvent('invoice.payment_failed', { id: 'in_1', subscription: STRIPE_SUB }, { created: 1_761_700_000 }));

    expect(res.status).toBe(200);
    expect(db.all(SUB_UPDATE)).toHaveLength(1);
    const { sql, params } = db.find(SUB_UPDATE)!;
    expect(sql).toMatch(/SET status = 'past_due',/);
    // A failed invoice only takes a subscription that is in force to past_due.
    // It used to apply to anything but `canceled`, which would have lifted an
    // `unpaid` or `incomplete` subscription to `past_due`, a status that publishes.
    expect(sql).toMatch(/WHERE stripe_subscription_id = \$1\s+AND status IN \('active', 'trialing', 'past_due'\)\s+AND \(last_event_created IS NULL OR last_event_created <= \$2\)/);
    expect(sql).toMatch(/SET subscription_status = 'past_due'[\s\S]*AND k\.stripe_subscription_id = \$1\s+AND k\.subscription_status IN \('active', 'trialing', 'past_due'\)/);
    expect(params).toEqual([STRIPE_SUB, 1_761_700_000]);
    expect(db.all(/^\s*UPDATE keepers/)).toHaveLength(0);
  });

  it('invoice.payment_failed for an invoice this database knows nothing about is acknowledged', async () => {
    withOrderingColumn(true);
    db.on(SUB_UPDATE, { rows: [{ applied: 0, mirrored: 0 }] });
    const res = await deliver(stripeEvent('invoice.payment_failed', { id: 'in_2', subscription: 'sub_someone_else' }));
    expect(res.status).toBe(200);
    expect(db.statements[db.statements.length - 1].sql).toBe('COMMIT');
  });

  it('invoice.payment_failed without a subscription id is acknowledged without a query', async () => {
    const res = await deliver(stripeEvent('invoice.payment_failed', { id: 'in_3' }));
    expect(res.status).toBe(200);
    expect(db.find(SUB_UPDATE)).toBeUndefined();
  });
});

// --- POST /webhooks/build-status: additional auth tests ---

describe('POST /webhooks/build-status: auth edge cases', () => {
  beforeEach(() => {
    mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
  });

  it('returns 401 when Authorization header is missing entirely', async () => {
    const res = await request(app)
      .post('/webhooks/build-status')
      .send({ build_id: 'b-1', status: 'live' });

    expect(res.status).toBe(401);
  });

  it('returns 500 when WORKER_WEBHOOK_SECRET is not set', async () => {
    const original = process.env.WORKER_WEBHOOK_SECRET;
    delete process.env.WORKER_WEBHOOK_SECRET;

    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer something')
      .send({ build_id: 'b-1', status: 'live' });

    expect(res.status).toBe(500);
    expect(res.body.error).toMatch(/not configured/i);

    process.env.WORKER_WEBHOOK_SECRET = original;
  });

  it('returns 500 when DB update throws (build-status)', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'build-db-err',
        app_name: 'ErrApp',
        current_status: 'building',
        email: 'err@example.com',
        name: 'Err',
      }],
    });
    mockQuery.mockRejectedValueOnce(new Error('DB write failed'));

    const res = await request(app)
      .post('/webhooks/build-status')
      .set('Authorization', 'Bearer test-webhook-secret')
      .send({ build_id: 'build-db-err', status: 'building' });

    expect(res.status).toBe(500);
  });
});

// --- POST /webhooks/resend (email delivery tracking) ---

// The signing key is assembled at runtime so no secret-shaped literal sits in
// the repo. Svix secrets are "whsec_" + base64(key).
const RESEND_KEY_BYTES = crypto.createHash('sha256').update('resend-webhook-test-key').digest();
const RESEND_SECRET = ['whsec', RESEND_KEY_BYTES.toString('base64')].join('_');

function signResend(rawBody: string, opts: { id?: string; timestamp?: number; key?: Buffer } = {}) {
  const id = opts.id ?? 'msg_test_1';
  const timestamp = String(opts.timestamp ?? Math.floor(Date.now() / 1000));
  const signature = crypto
    .createHmac('sha256', opts.key ?? RESEND_KEY_BYTES)
    .update(`${id}.${timestamp}.${rawBody}`)
    .digest('base64');
  return { 'svix-id': id, 'svix-timestamp': timestamp, 'svix-signature': `v1,${signature}` };
}

/** POST a raw JSON string to /webhooks/resend with a valid signature unless headers are overridden. */
function postResend(rawBody: string, headers: Record<string, string> = signResend(rawBody)) {
  let req = request(app).post('/webhooks/resend').set('Content-Type', 'application/json');
  for (const [name, value] of Object.entries(headers)) req = req.set(name, value);
  return req.send(rawBody);
}

describe('POST /webhooks/resend', () => {
  beforeEach(() => {
    mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
    process.env.RESEND_WEBHOOK_SECRET = RESEND_SECRET;
  });

  it('fails closed when RESEND_WEBHOOK_SECRET is not configured', async () => {
    delete process.env.RESEND_WEBHOOK_SECRET;

    const res = await request(app)
      .post('/webhooks/resend')
      .send({ type: 'email.bounced', data: { email_id: 'resend-id-123' } });

    expect(res.status).toBe(500);
    expect(res.body.error).toMatch(/not configured/i);
    // An unauthenticated caller must not be able to rewrite delivery status.
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('returns 400 when payload is missing type or email_id', async () => {
    const res = await postResend(JSON.stringify({ type: 'email.delivered' }));

    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/Invalid/);
  });

  it('updates delivery_status for a delivered event', async () => {
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

    const res = await postResend(
      JSON.stringify({ type: 'email.delivered', data: { email_id: 'resend-id-123', to: ['user@example.com'] } })
    );

    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);

    const updateCall = mockQuery.mock.calls[0];
    expect(updateCall[0]).toContain('UPDATE email_log');
    expect(updateCall[0]).toContain('delivery_status');
    expect(updateCall[1][0]).toBe('delivered');
    expect(updateCall[1][1]).toBe('resend-id-123');
  });

  it('updates delivery_status for a bounced event', async () => {
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

    const res = await postResend(
      JSON.stringify({ type: 'email.bounced', data: { email_id: 'resend-bounce-1', to: ['bad@example.com'] } })
    );

    expect(res.status).toBe(200);
    const updateCall = mockQuery.mock.calls[0];
    expect(updateCall[1][0]).toBe('bounced');
  });

  it('ignores untracked event types (e.g. email.sent)', async () => {
    const res = await postResend(JSON.stringify({ type: 'email.sent', data: { email_id: 'resend-sent-1' } }));

    expect(res.status).toBe(200);
    expect(res.body.ignored).toBe(true);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('returns 401 when signature verification is required but headers are missing', async () => {
    const res = await request(app)
      .post('/webhooks/resend')
      .send({ type: 'email.delivered', data: { email_id: 'id-1' } });

    expect(res.status).toBe(401);
    expect(res.body.error).toMatch(/signature/i);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('verifies the bytes that were sent, not a re-serialization of the parsed JSON', async () => {
    // Whitespace and an escaped character: JSON.parse then JSON.stringify
    // would produce different bytes and the signature would not match.
    const rawBody = '{ "type" : "email.delivered",\n  "data": { "email_id": "id-raw-1", "note": "caf\\u00e9" } }';
    expect(JSON.stringify(JSON.parse(rawBody))).not.toBe(rawBody);
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

    const res = await postResend(rawBody);

    expect(res.status).toBe(200);
    expect(mockQuery.mock.calls[0][1][1]).toBe('id-raw-1');
  });

  it('rejects a body that was changed after signing', async () => {
    const signed = JSON.stringify({ type: 'email.delivered', data: { email_id: 'id-1' } });
    const tampered = JSON.stringify({ type: 'email.bounced', data: { email_id: 'id-1' } });

    const res = await postResend(tampered, signResend(signed));

    expect(res.status).toBe(401);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('rejects a signature made with a different key', async () => {
    const rawBody = JSON.stringify({ type: 'email.delivered', data: { email_id: 'id-1' } });
    const otherKey = crypto.createHash('sha256').update('some-other-key').digest();

    const res = await postResend(rawBody, signResend(rawBody, { key: otherKey }));

    expect(res.status).toBe(401);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('rejects a correctly signed delivery whose timestamp is outside the tolerance (replay)', async () => {
    const rawBody = JSON.stringify({ type: 'email.delivered', data: { email_id: 'id-1' } });
    const tenMinutesAgo = Math.floor(Date.now() / 1000) - 10 * 60;

    const res = await postResend(rawBody, signResend(rawBody, { timestamp: tenMinutesAgo }));

    expect(res.status).toBe(401);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('returns 401 (not a thrown RangeError) for a signature with non-ASCII bytes', async () => {
    const rawBody = JSON.stringify({ type: 'email.delivered', data: { email_id: 'id-1' } });
    const headers = signResend(rawBody);
    // 44 characters like a real base64 SHA-256 signature, but more bytes.
    headers['svix-signature'] = 'v1,' + '\u00e9'.repeat(44);

    const res = await postResend(rawBody, headers);

    expect(res.status).toBe(401);
  });

  it('accepts any one valid entry in a multi-signature header (secret rotation)', async () => {
    const rawBody = JSON.stringify({ type: 'email.delivered', data: { email_id: 'id-rot-1' } });
    const headers = signResend(rawBody);
    headers['svix-signature'] = `v1,${'A'.repeat(43)}= ${headers['svix-signature']}`;
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [] });

    const res = await postResend(rawBody, headers);

    expect(res.status).toBe(200);
  });

  it('handles DB errors gracefully', async () => {
    mockQuery.mockRejectedValueOnce(new Error('DB connection lost'));

    const res = await postResend(JSON.stringify({ type: 'email.delivered', data: { email_id: 'id-err' } }));

    expect(res.status).toBe(500);
  });
});

describe('verifySvixSignature', () => {
  const payload = '{"type":"email.delivered"}';
  const now = 1_700_000_000;
  const sign = (timestamp: number) =>
    'v1,' + crypto.createHmac('sha256', RESEND_KEY_BYTES).update(`msg_1.${timestamp}.${payload}`).digest('base64');

  it('accepts a valid signature inside the tolerance window', () => {
    expect(verifySvixSignature({
      secret: RESEND_SECRET, id: 'msg_1', timestamp: String(now - 60), signatureHeader: sign(now - 60), payload, nowSeconds: now,
    })).toBe('ok');
  });

  it('reports missing headers, stale timestamps, and bad signatures distinctly', () => {
    expect(verifySvixSignature({
      secret: RESEND_SECRET, id: undefined, timestamp: String(now), signatureHeader: sign(now), payload, nowSeconds: now,
    })).toBe('missing');
    expect(verifySvixSignature({
      secret: RESEND_SECRET, id: 'msg_1', timestamp: String(now - 301), signatureHeader: sign(now - 301), payload, nowSeconds: now,
    })).toBe('stale');
    expect(verifySvixSignature({
      secret: RESEND_SECRET, id: 'msg_1', timestamp: String(now + 301), signatureHeader: sign(now + 301), payload, nowSeconds: now,
    })).toBe('stale');
    expect(verifySvixSignature({
      secret: RESEND_SECRET, id: 'msg_1', timestamp: 'not-a-number', signatureHeader: sign(now), payload, nowSeconds: now,
    })).toBe('stale');
    expect(verifySvixSignature({
      secret: RESEND_SECRET, id: 'msg_2', timestamp: String(now), signatureHeader: sign(now), payload, nowSeconds: now,
    })).toBe('invalid');
  });

  it('never accepts an array-valued header', () => {
    expect(verifySvixSignature({
      secret: RESEND_SECRET, id: ['msg_1'], timestamp: String(now), signatureHeader: sign(now), payload, nowSeconds: now,
    })).toBe('missing');
  });
});
