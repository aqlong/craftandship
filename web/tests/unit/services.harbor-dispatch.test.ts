import { describe, it, expect, vi, beforeEach } from 'vitest';

// The model call is stood in for; the module's error class and readiness
// helpers are the real ones.
vi.mock('../../src/services/spec_extraction', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/services/spec_extraction')>()),
  extractSpecFromConversation: vi.fn(),
}));
// The translation is stood in for. The address rule is not: it is the real
// isEngineSafeAdminEmail, the function the dispatch asks and the one
// services.harbor-to-compose.test.ts compares with the engine's own pattern.
vi.mock('../../src/services/harbor_to_compose', async (importOriginal) => ({
  isEngineSafeAdminEmail: (await importOriginal<typeof import('../../src/services/harbor_to_compose')>()).isEngineSafeAdminEmail,
  translateExtractedSpecToV1: vi.fn(),
}));
vi.mock('bcrypt', () => ({
  default: { hash: vi.fn().mockResolvedValue('hashed') },
  hash: vi.fn().mockResolvedValue('hashed'),
}));

import {
  dispatchHarborTrialBuild,
  dispatchHarborKeeperBuild,
  dispatchFailureResponse,
  insertHarborBuild,
  HarborDispatchError,
  isContentFreeSpec,
  isBuildableOwnerEmail,
  HARBOR_DISPATCH_START_KIND,
  OWNER_EMAIL_NOT_BUILDABLE_MESSAGE,
} from '../../src/services/harbor_dispatch';
import {
  BUILD_START_LOCK_SQL,
  MAX_ACTIVE_BUILDS_PER_CUSTOMER,
  MAX_BUILD_STARTS_PER_DAY,
} from '../../src/services/build-start-limits';
import {
  LlmDailyBudgetError,
  anonymousCallBudget,
  anonymousCallsAdmitted,
  resetDailyCallBudget,
  tryAdmitAnonymousCalls,
} from '../../src/services/llm-budget';
import { extractSpecFromConversation, SpecExtractionError } from '../../src/services/spec_extraction';
import { translateExtractedSpecToV1 } from '../../src/services/harbor_to_compose';
import { AssistantNotConfiguredError, respondAssistantNotConfigured } from '../../src/utils/assistant';

const mockExtract = extractSpecFromConversation as unknown as ReturnType<typeof vi.fn>;
const mockTranslate = translateExtractedSpecToV1 as unknown as ReturnType<typeof vi.fn>;

/**
 * A pool double. `query` and `clientQuery` are scripted by each test, in
 * order, exactly as before the build-start limits existed: the statements the
 * limits added are answered here instead, so a test that is not about them
 * does not have to script them.
 *
 *  - the early "does this address have an account" read (before the upsert)
 *    answers `knownAccount` (default: no account);
 *  - the budget read answers `budget` (default: nothing active, no starts);
 *  - the advisory lock and the ledger insert succeed;
 *  - the account's verification, read again under a share lock in the insert
 *    transaction, answers `verifiedUnderLock` (default: not verified).
 *
 * `allCalls` and `allClientCalls` record every statement, these included.
 */
function makePool(
  opts: {
    knownAccount?: { id: string; email_verified: boolean } | null;
    budget?: { active: number; starts_today: number };
    verifiedUnderLock?: boolean;
  } = {}
) {
  const query = vi.fn();
  const clientQuery = vi.fn().mockResolvedValue({ rows: [] });
  const allCalls: Array<[string, unknown[] | undefined]> = [];
  const allClientCalls: Array<[string, unknown[] | undefined]> = [];
  const budgetRow = { rows: [opts.budget ?? { active: 0, starts_today: 0 }] };
  let upsertSeen = false;
  const limitsAnswer = (sql: string): unknown => {
    if (/pg_advisory_xact_lock/.test(sql)) return { rows: [] };
    if (/FROM build_start_events/.test(sql)) return budgetRow;
    if (/INSERT INTO build_start_events/.test(sql)) return { rows: [] };
    if (/SELECT email_verified FROM customers WHERE id = \$1 FOR SHARE/.test(sql)) {
      return { rows: [{ email_verified: opts.verifiedUnderLock === true }] };
    }
    return undefined;
  };
  const poolQuery = vi.fn(async (sql: string, params?: unknown[]) => {
    allCalls.push([sql, params]);
    if (/INSERT INTO customers/.test(sql)) upsertSeen = true;
    if (!upsertSeen && /SELECT id, email_verified FROM customers WHERE email = \$1/.test(sql)) {
      return { rows: opts.knownAccount ? [opts.knownAccount] : [] };
    }
    return limitsAnswer(sql) ?? query(sql, params);
  });
  const client = {
    query: vi.fn(async (sql: string, params?: unknown[]) => {
      allClientCalls.push([sql, params]);
      return limitsAnswer(sql) ?? clientQuery(sql, params);
    }),
    release: vi.fn(),
  };
  const connect = vi.fn().mockResolvedValue(client);
  return { pool: { query: poolQuery, connect } as any, query, clientQuery, client, connect, allCalls, allClientCalls };
}

const TRIAL = 'a11c0de0-0000-4000-8000-000000000001';

function trialRow(over: Record<string, unknown> = {}) {
  return {
    trial_id: TRIAL,
    email_captured: 'maya@example.com',
    handoff_build_id: null,
    released_at: null,
    harbor_meta_overrides: null,
    ...over,
  };
}

describe('dispatchHarborTrialBuild', () => {
  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: [], costMicros: 0 });
    mockTranslate.mockReturnValue({ spec: { app_name: 'Maya Reiki', tier: 'build' }, warnings: [] });
  });

  it('throws trial_not_found when the trial does not exist', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [] }); // trial load
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'a@example.test', source: 'self_serve' })
    ).rejects.toMatchObject({ code: 'trial_not_found' });
  });

  it('is idempotent: returns the attached build without re-extracting', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow({ handoff_build_id: 'existing' })] });
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'a@example.test', source: 'self_serve' });
    expect(res).toMatchObject({ buildId: 'existing', alreadyDispatched: true });
    expect(mockExtract).not.toHaveBeenCalled();
  });

  it('throws trial_already_promoted when released_at is set', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow({ released_at: new Date() })] });
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'a@example.test', source: 'self_serve' })
    ).rejects.toMatchObject({ code: 'trial_already_promoted' });
  });

  it('throws no_email when resolvedEmail is empty', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: '   ', source: 'self_serve' })
    ).rejects.toMatchObject({ code: 'no_email' });
  });

  it('refuses a BARREN_SPEC when refuseBarrenSpec is set (no build created)', async () => {
    const { pool, query, connect } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] }); // trial load
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] }); // customer upsert
    mockTranslate.mockReturnValueOnce({ spec: { app_name: 'X' }, warnings: ['BARREN_SPEC: 0 of 8 sections handled'] });
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'a@example.test', source: 'self_serve', refuseBarrenSpec: true })
    ).rejects.toMatchObject({ code: 'barren_spec' });
    expect(connect).not.toHaveBeenCalled(); // never opened the insert transaction
  });

  it('happy path: upserts customer, extracts, inserts build + stamps trial', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] }); // trial load
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] }); // customer upsert
    // A self-serve build needs at least one real signal from the keeper.
    mockTranslate.mockReturnValueOnce({
      spec: { app_name: 'Maya Reiki', tier: 'build', _seed_settings: { about_body: 'I teach breathwork.' } },
      warnings: [],
    });
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] }) // SELECT ... FOR UPDATE
      .mockResolvedValueOnce({ rows: [{ id: 'build-1' }] }) // INSERT build
      .mockResolvedValueOnce({}) // UPDATE trial
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve', refuseBarrenSpec: true });
    expect(res).toMatchObject({ buildId: 'build-1', alreadyDispatched: false, tier: 'build', customerId: 'cust-1' });
    // build inserted at status='queued', and marked as a Harbor build: the
    // column the customer's publish gate reads.
    const insertCall = clientQuery.mock.calls.find((c) => /INSERT INTO builds/.test(c[0]));
    expect(insertCall[0]).toMatch(
      /\(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)\s+VALUES \(\$1, \$2, 'queued', \$3, \$4, TRUE, \$5\)/
    );
    // trial stamped with handoff_build_id, and with handoff_at in the same statement
    const updateCall = clientQuery.mock.calls.find((c) => /UPDATE trial_keepers/.test(c[0]));
    expect(updateCall[0]).toMatch(/SET handoff_build_id = \$1, handoff_at = NOW\(\)\s+WHERE trial_id = \$2 AND handoff_build_id IS NULL/);
    expect(updateCall[1]).toEqual(['build-1', TRIAL]);
  });

  describe('one build per trial, even after that build is deleted', () => {
    // trial_keepers.handoff_build_id is ON DELETE SET NULL, and the worker
    // deletes a customer's older failed builds. handoff_at stays.
    const used = { handoff_build_id: null, handoff_at: new Date('2026-05-01T00:00:00Z') };

    it('refuses a trial whose build was dispatched and is gone, before the paid extraction', async () => {
      const { pool, query, connect } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow(used)] });
      await expect(
        dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve', refuseBarrenSpec: true })
      ).rejects.toMatchObject({ code: 'build_already_used' });
      expect(mockExtract).not.toHaveBeenCalled();
      expect(connect).not.toHaveBeenCalled();
      expect(query.mock.calls.some((c) => /INSERT INTO customers/.test(String(c[0])))).toBe(false);
    });

    it('applies the same rule under the row lock (the unlocked read can be stale)', async () => {
      const { pool, query, clientQuery, client } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow()] }); // unlocked read: never dispatched
      query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
      mockTranslate.mockReturnValueOnce({
        spec: { app_name: 'Maya Reiki', tier: 'build', _seed_settings: { about_body: 'I teach breathwork.' } },
        warnings: [],
      });
      clientQuery
        .mockResolvedValueOnce({}) // BEGIN
        .mockResolvedValueOnce({ rows: [used] }) // SELECT ... FOR UPDATE: dispatched and deleted in the meantime
        .mockResolvedValueOnce({}); // ROLLBACK
      await expect(
        dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve', refuseBarrenSpec: true })
      ).rejects.toMatchObject({ code: 'build_already_used' });
      expect(clientQuery.mock.calls.some((c) => /INSERT INTO builds/.test(c[0]))).toBe(false);
      expect(clientQuery.mock.calls.some((c) => c[0] === 'ROLLBACK')).toBe(true);
      expect(client.release).toHaveBeenCalled();
    });

    it('an operator may dispatch again (allowRedispatch)', async () => {
      const { pool, query, clientQuery } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow(used)] });
      query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
      clientQuery
        .mockResolvedValueOnce({}) // BEGIN
        .mockResolvedValueOnce({ rows: [used] }) // SELECT ... FOR UPDATE
        .mockResolvedValueOnce({ rows: [{ id: 'build-again' }] }) // INSERT build
        .mockResolvedValueOnce({}) // UPDATE trial
        .mockResolvedValueOnce({}); // COMMIT
      const res = await dispatchHarborTrialBuild(pool, {
        trialId: TRIAL,
        resolvedEmail: 'maya@example.com',
        source: 'admin',
        allowRedispatch: true,
      });
      expect(res).toMatchObject({ buildId: 'build-again', alreadyDispatched: false });
    });

    it('a trial row that vanished before the lock inserts nothing', async () => {
      const { pool, query, clientQuery } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow()] });
      query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
      clientQuery
        .mockResolvedValueOnce({}) // BEGIN
        .mockResolvedValueOnce({ rows: [] }) // SELECT ... FOR UPDATE: no such trial
        .mockResolvedValueOnce({}); // ROLLBACK
      await expect(
        dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' })
      ).rejects.toMatchObject({ code: 'trial_not_found' });
      expect(clientQuery.mock.calls.some((c) => /INSERT INTO builds/.test(c[0]))).toBe(false);
    });
  });

  it('customer upsert race: ON CONFLICT returns no row -> re-SELECTs existing', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] }); // trial load
    query.mockResolvedValueOnce({ rows: [] }); // upsert ON CONFLICT DO NOTHING -> no row
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-existing' }] }); // re-SELECT
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] }) // SELECT ... FOR UPDATE
      .mockResolvedValueOnce({ rows: [{ id: 'build-2' }] }) // INSERT build
      .mockResolvedValueOnce({}) // UPDATE trial
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });
    expect(res.customerId).toBe('cust-existing');
    expect(res.buildId).toBe('build-2');
  });

  it('refuses to attach a self-serve build to an existing VERIFIED account (typed email is not proof)', async () => {
    const { pool, query, connect } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow({ email_captured: 'victim@example.com' })] }); // trial load
    query.mockResolvedValueOnce({ rows: [] }); // upsert ON CONFLICT DO NOTHING -> the address has an account
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-victim', email_verified: true }] }); // re-SELECT
    await expect(
      dispatchHarborTrialBuild(pool, {
        trialId: TRIAL,
        resolvedEmail: 'victim@example.com',
        source: 'self_serve',
        refuseBarrenSpec: false,
      })
    ).rejects.toMatchObject({ code: 'account_exists' });
    // No transaction was opened, so no build row and no handoff stamp exist.
    expect(connect).not.toHaveBeenCalled();
    const sqls = query.mock.calls.map((c) => String(c[0]));
    expect(sqls.some((q) => /INSERT INTO builds/.test(q))).toBe(false);
  });

  it('a dispatch that is not anonymous still attaches to an existing account that nobody has verified', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    query.mockResolvedValueOnce({ rows: [] });
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-unverified', email_verified: false }] });
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-3' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });
    expect(res).toMatchObject({ customerId: 'cust-unverified', buildId: 'build-3' });
  });

  it('an operator-chosen address may be a verified account (allowVerifiedAccount)', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    query.mockResolvedValueOnce({ rows: [] });
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-operator', email_verified: true }] });
    clientQuery
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-4' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(pool, {
      trialId: TRIAL,
      resolvedEmail: 'owner@example.com',
      source: 'admin',
      allowVerifiedAccount: true,
    });
    expect(res).toMatchObject({ customerId: 'cust-operator', buildId: 'build-4' });
  });

  it('coerces a hallucinated tier to "build" with a warning', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
    mockTranslate.mockReturnValueOnce({ spec: { app_name: 'X', tier: 'music-instructor' }, warnings: [] });
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] }) // SELECT ... FOR UPDATE
      .mockResolvedValueOnce({ rows: [{ id: 'build-3' }] }) // INSERT build
      .mockResolvedValueOnce({}) // UPDATE trial
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'a@example.test', source: 'self_serve' });
    expect(res.tier).toBe('build');
    expect(res.warnings.some((w) => /coerced to 'build'/.test(w))).toBe(true);
  });

  it('rolls back + releases the client when the build INSERT fails', async () => {
    const { pool, query, clientQuery, client } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] }) // SELECT ... FOR UPDATE
      .mockRejectedValueOnce(new Error('insert boom')); // INSERT fails
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'a@example.test', source: 'self_serve' })
    ).rejects.toThrow('insert boom');
    expect(clientQuery.mock.calls.some((c) => c[0] === 'ROLLBACK')).toBe(true);
    expect(client.release).toHaveBeenCalled();
  });

  it('concurrent dispatch race: a sibling already stamped handoff -> returns it, no second build', async () => {
    // Two dispatches for the same trial both passed the unlocked step-2
    // idempotency read and both ran the Opus extraction. The loser re-checks
    // handoff_build_id under FOR UPDATE inside the transaction, sees the
    // winner's stamp, and must NOT insert a second build.
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] }); // trial load (still null at step 2)
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] }); // customer upsert
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: 'winner-build' }] }) // SELECT ... FOR UPDATE sees the winner
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });
    expect(res).toMatchObject({ buildId: 'winner-build', alreadyDispatched: true });
    // critical: no second build was inserted
    expect(clientQuery.mock.calls.some((c) => /INSERT INTO builds/.test(c[0]))).toBe(false);
  });
});

describe('what can refuse a trial dispatch is asked before the extraction is paid for', () => {
  const ready = {
    spec: { app_name: 'Maya Reiki', tier: 'build', _seed_settings: { about_body: 'I teach breathwork.' } },
    warnings: [],
  };

  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: [], costMicros: 0 });
    mockTranslate.mockReturnValue(ready);
    resetDailyCallBudget();
  });

  it('an address that already has a VERIFIED account: refused with no model call and no customers write', async () => {
    const { pool, query, connect, allCalls } = makePool({ knownAccount: { id: 'cust-victim', email_verified: true } });
    query.mockResolvedValueOnce({ rows: [trialRow({ email_captured: 'victim@example.com' })] });
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'Victim@Example.com', source: 'self_serve', refuseBarrenSpec: true })
    ).rejects.toMatchObject({ code: 'account_exists' });
    expect(mockExtract).not.toHaveBeenCalled();
    expect(connect).not.toHaveBeenCalled();
    expect(allCalls.some(([sql]) => /INSERT INTO customers/.test(sql))).toBe(false);
    // The lookup used the normalized address.
    const lookup = allCalls.find(([sql]) => /FROM customers WHERE email = \$1/.test(sql));
    expect(lookup?.[1]).toEqual(['victim@example.com']);
    // The error's own text is for logs, and the route does not send it.
    await expect(
      (async () => {
        const again = makePool({ knownAccount: { id: 'cust-victim', email_verified: true } });
        again.query.mockResolvedValueOnce({ rows: [trialRow()] });
        return dispatchHarborTrialBuild(again.pool, { trialId: TRIAL, resolvedEmail: 'victim@example.com', source: 'self_serve' });
      })()
    ).rejects.toThrow(/verified account/);
  });

  it('the caller that proves the account (allowVerifiedAccount) is not refused by that check', async () => {
    const { pool, query, clientQuery } = makePool({ knownAccount: { id: 'cust-own', email_verified: true } });
    query.mockResolvedValueOnce({ rows: [trialRow()] }); // trial load
    query.mockResolvedValueOnce({ rows: [] }); // upsert: the row exists
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-own', email_verified: true }] }); // re-SELECT
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-own' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(pool, {
      trialId: TRIAL,
      resolvedEmail: 'own@example.com',
      source: 'self_serve',
      allowVerifiedAccount: true,
    });
    expect(res).toMatchObject({ buildId: 'build-own', customerId: 'cust-own' });
  });

  it.each([
    ['two builds already queued or in progress', { active: 2, starts_today: 0 }, /already have builds in progress/],
    ['ten starts in the last 24 hours', { active: 0, starts_today: 10 }, /limit for new builds, rebuilds, revisions and retries/],
  ])('a customer at a build-start limit (%s): refused with no model call', async (_label, budget, message) => {
    const { pool, query, connect } = makePool({ knownAccount: { id: 'cust-busy', email_verified: false }, budget });
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    const attempt = dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });
    await expect(attempt).rejects.toMatchObject({ code: 'build_limit' });
    await expect(attempt).rejects.toThrow(message);
    expect(mockExtract).not.toHaveBeenCalled();
    expect(connect).not.toHaveBeenCalled();
  });

  it('the limits are the shared ones, which routes/builds.ts re-exports (one below each is accepted)', async () => {
    expect(MAX_ACTIVE_BUILDS_PER_CUSTOMER).toBe(2);
    expect(MAX_BUILD_STARTS_PER_DAY).toBe(10);
    expect(HARBOR_DISPATCH_START_KIND).toBe('dispatch');
    const { pool, query, clientQuery } = makePool({
      knownAccount: { id: 'cust-busy', email_verified: false },
      budget: { active: MAX_ACTIVE_BUILDS_PER_CUSTOMER - 1, starts_today: MAX_BUILD_STARTS_PER_DAY - 1 },
    });
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    query.mockResolvedValueOnce({ rows: [] });
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-busy', email_verified: false }] });
    clientQuery
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-ok' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });
    expect(res.buildId).toBe('build-ok');
  });

  it('an anonymous dispatch is refused when anonymous visitors have used their share of the model-call budget', async () => {
    const saved = process.env.CAS_LLM_DAILY_CALL_CAP;
    process.env.CAS_LLM_DAILY_CALL_CAP = '5'; // anonymous share: 4
    try {
      expect(anonymousCallBudget()).toBe(4);
      expect(tryAdmitAnonymousCalls(4)).toBe(true);
      const { pool, query, connect } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow()] });
      await expect(
        dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve', anonymous: true })
      ).rejects.toBeInstanceOf(LlmDailyBudgetError);
      expect(mockExtract).not.toHaveBeenCalled();
      expect(connect).not.toHaveBeenCalled();

      // The same dispatch by a caller with an account is not held to that share.
      const second = makePool();
      second.query.mockResolvedValueOnce({ rows: [trialRow()] });
      second.query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
      second.clientQuery
        .mockResolvedValueOnce({})
        .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
        .mockResolvedValueOnce({ rows: [{ id: 'build-acct' }] })
        .mockResolvedValueOnce({})
        .mockResolvedValueOnce({});
      const res = await dispatchHarborTrialBuild(second.pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });
      expect(res.buildId).toBe('build-acct');
    } finally {
      if (saved === undefined) delete process.env.CAS_LLM_DAILY_CALL_CAP;
      else process.env.CAS_LLM_DAILY_CALL_CAP = saved;
      resetDailyCallBudget();
    }
  });

  it('a refusal that needs no model does not use up an anonymous slot', async () => {
    const { pool, query } = makePool({ knownAccount: { id: 'cust-victim', email_verified: true } });
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'victim@example.com', source: 'self_serve', anonymous: true })
    ).rejects.toMatchObject({ code: 'account_exists' });
    expect(anonymousCallsAdmitted()).toBe(0);
  });

  describe('the anonymous slot is for one model call: given back when the call was not made, kept when it was', () => {
    const anonymousDispatch = (pool: unknown) =>
      dispatchHarborTrialBuild(pool as never, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve', anonymous: true });

    it.each([
      ['nothing to extract', () => new SpecExtractionError('nothing_to_extract', 'nothing')],
      ['no model key on this server', () => new AssistantNotConfiguredError()],
      ['the daily pool already spent', () => new LlmDailyBudgetError()],
      ['a database error while the conversation is loaded', () => new Error('connection lost')],
    ])('%s: the extraction ended before the call, and the slot is free again', async (_label, failure) => {
      const { pool, query, connect } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow()] });
      // The extraction rejects without ever announcing a model call.
      mockExtract.mockRejectedValueOnce(failure());
      await expect(anonymousDispatch(pool)).rejects.toBeInstanceOf(Error);
      expect(mockExtract).toHaveBeenCalledTimes(1);
      expect(anonymousCallsAdmitted()).toBe(0);
      expect(connect).not.toHaveBeenCalled();
    });

    it.each([
      ['a reply that is not a spec', () => new SpecExtractionError('unusable_reply', 'unusable')],
      ['a call the provider did not answer', () => new SpecExtractionError('model_call_failed', 'failed')],
    ])('%s: the call was made, and the slot stays taken', async (_label, failure) => {
      const { pool, query, connect, allCalls } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow()] });
      // The extraction announces the call (as the real one does, immediately
      // before the request) and then fails.
      mockExtract.mockImplementationOnce(async (opts: { onModelCall?: () => void }) => {
        opts.onModelCall?.();
        throw failure();
      });
      await expect(anonymousDispatch(pool)).rejects.toBeInstanceOf(SpecExtractionError);
      expect(anonymousCallsAdmitted()).toBe(1);
      // Nothing was written: no account for the typed address, no transaction.
      expect(allCalls.some(([sql]) => /INSERT INTO/.test(sql))).toBe(false);
      expect(connect).not.toHaveBeenCalled();
    });

    it('a refusal after a good reply (a barren spec) keeps the slot too: that call was paid for', async () => {
      const { pool, query } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow()] });
      mockExtract.mockImplementationOnce(async (opts: { onModelCall?: () => void }) => {
        opts.onModelCall?.();
        return { spec: { app_name: 'x', pages: [] }, warnings: [], costMicros: 1, contextSize: { memoryRows: 0, turns: 1 } };
      });
      mockTranslate.mockReturnValueOnce({ spec: { app_name: 'x', tier: 'build' }, warnings: ['BARREN_SPEC: 0 of 3 sections handled'] });
      await expect(
        dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve', anonymous: true, refuseBarrenSpec: true })
      ).rejects.toMatchObject({ code: 'barren_spec' });
      expect(anonymousCallsAdmitted()).toBe(1);
    });

    it('a caller with an account takes no slot and gives none back', async () => {
      expect(tryAdmitAnonymousCalls(2)).toBe(true);
      const { pool, query } = makePool();
      query.mockResolvedValueOnce({ rows: [trialRow()] });
      mockExtract.mockRejectedValueOnce(new SpecExtractionError('nothing_to_extract', 'nothing'));
      await expect(
        dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' })
      ).rejects.toMatchObject({ code: 'nothing_to_extract' });
      expect(anonymousCallsAdmitted()).toBe(2);
    });
  });
});

describe('dispatchFailureResponse: one answer per failure, for the visitor, the keeper and the operator', () => {
  it('every refusal of the dispatch has a status, and the body is its code and its sentence', () => {
    const expected: Record<string, number> = {
      trial_not_found: 404,
      keeper_not_found: 404,
      trial_already_promoted: 409,
      no_email: 402,
      barren_spec: 409,
      account_exists: 409,
      email_not_buildable: 409,
      build_already_used: 409,
      build_limit: 429,
    };
    for (const [code, status] of Object.entries(expected)) {
      expect(dispatchFailureResponse(new HarborDispatchError(code as never, `about ${code}`)), code).toEqual({
        status,
        body: { error: code, message: `about ${code}` },
      });
    }
  });

  it('the extraction\'s failures: nothing to read is 409, a failed call and an unusable reply are 502', () => {
    expect(dispatchFailureResponse(new SpecExtractionError('nothing_to_extract', 'a'))).toEqual({ status: 409, body: { error: 'nothing_to_extract', message: 'a' } });
    expect(dispatchFailureResponse(new SpecExtractionError('model_call_failed', 'b'))).toEqual({ status: 502, body: { error: 'model_call_failed', message: 'b' } });
    expect(dispatchFailureResponse(new SpecExtractionError('unusable_reply', 'c'))).toEqual({ status: 502, body: { error: 'unusable_reply', message: 'c' } });
  });

  it('a spent budget is 503 "busy", and no model key is the answer the chat gives', () => {
    const busy = dispatchFailureResponse(new LlmDailyBudgetError());
    expect(busy?.status).toBe(503);
    expect(busy?.body.error).toBe('busy');
    expect(busy?.body.message).toMatch(/try again later/i);

    const sent: { status?: number; body?: unknown } = {};
    respondAssistantNotConfigured({
      status(code: number) {
        sent.status = code;
        return this;
      },
      json(body: unknown) {
        sent.body = body;
        return this;
      },
    } as never);
    expect(dispatchFailureResponse(new AssistantNotConfiguredError())).toEqual({ status: sent.status, body: sent.body });
    expect(sent.status).toBe(503);
  });

  it('anything else is not mapped: a server fault is for the error handler', () => {
    for (const fault of [new Error('database down'), new TypeError('x is not a function'), 'a string', null, undefined, { code: 'barren_spec' }]) {
      expect(dispatchFailureResponse(fault)).toBeNull();
    }
  });
});

describe('a dispatch is a build start: the lock, the limits and the ledger, in one transaction', () => {
  const KEEPER = 'c0ffee00-0000-4000-8000-000000000002';

  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: [], costMicros: 0 });
    mockTranslate.mockReturnValue({
      spec: { app_name: 'Maya Reiki', tier: 'build', _seed_settings: { about_body: 'I teach breathwork.' } },
      warnings: [],
    });
  });

  function scriptTrialHappyPath(p: ReturnType<typeof makePool>): void {
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
    p.clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-1' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
  }

  it('trial: BEGIN, the customer lock, the row lock, the account read again, the counts, the insert, the stamp, the ledger row, COMMIT', async () => {
    const p = makePool();
    scriptTrialHappyPath(p);
    await dispatchHarborTrialBuild(p.pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });

    const order = p.allClientCalls.map(([sql]) =>
      sql === 'BEGIN' || sql === 'COMMIT'
        ? sql
        : /pg_advisory_xact_lock/.test(sql)
          ? 'customer-lock'
          : /FOR UPDATE/.test(sql)
            ? 'row-lock'
            : /FROM customers WHERE id = \$1 FOR SHARE/.test(sql)
              ? 'account-share-lock'
              : /FROM build_start_events/.test(sql)
                ? 'counts'
                : /INSERT INTO builds /.test(sql)
                  ? 'insert-build'
                  : /UPDATE trial_keepers/.test(sql)
                    ? 'stamp'
                    : /INSERT INTO build_start_events/.test(sql)
                      ? 'ledger'
                      : `other: ${sql}`
    );
    expect(order).toEqual(['BEGIN', 'customer-lock', 'row-lock', 'account-share-lock', 'counts', 'insert-build', 'stamp', 'ledger', 'COMMIT']);
    const lock = p.allClientCalls.find(([sql]) => /pg_advisory_xact_lock/.test(sql));
    expect(lock?.[0]).toBe(BUILD_START_LOCK_SQL);
    expect(lock?.[1]).toEqual(['cust-1']);
    const ledger = p.allClientCalls.find(([sql]) => /INSERT INTO build_start_events/.test(sql));
    expect(ledger?.[1]).toEqual(['cust-1', 'build-1', HARBOR_DISPATCH_START_KIND]);
  });

  it('the count under the lock is the one that decides: a customer who reached the limit during the extraction gets no build', async () => {
    // The early read (pool) sees room; the locked read (client) does not.
    const p = makePool();
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
    const lockedBudget = { rows: [{ active: 2, starts_today: 3 }] };
    p.client.query.mockImplementation(async (sql: string, params?: unknown[]) => {
      p.allClientCalls.push([sql, params]);
      if (/FROM build_start_events/.test(sql)) return lockedBudget;
      if (/FOR UPDATE/.test(sql)) return { rows: [{ handoff_build_id: null, handoff_at: null }] };
      return { rows: [] };
    });
    await expect(
      dispatchHarborTrialBuild(p.pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' })
    ).rejects.toMatchObject({ code: 'build_limit' });
    const sqls = p.allClientCalls.map(([sql]) => sql);
    expect(sqls.some((sql) => /INSERT INTO builds /.test(sql))).toBe(false);
    expect(sqls.some((sql) => /INSERT INTO build_start_events/.test(sql))).toBe(false);
    expect(sqls).toContain('ROLLBACK');
    expect(p.client.release).toHaveBeenCalled();
  });

  it('a concurrent loser (the build is already attached) writes no second ledger row', async () => {
    const p = makePool();
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
    p.clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: 'winner-build', handoff_at: new Date() }] })
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborTrialBuild(p.pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' });
    expect(res).toMatchObject({ buildId: 'winner-build', alreadyDispatched: true });
    expect(p.allClientCalls.some(([sql]) => /INSERT INTO build_start_events/.test(sql))).toBe(false);
  });

  it('an operator (skipStartLimits) takes no lock, is not counted and is not refused', async () => {
    const p = makePool({ knownAccount: { id: 'cust-1', email_verified: false }, budget: { active: 5, starts_today: 50 } });
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.query.mockResolvedValueOnce({ rows: [] });
    p.query.mockResolvedValueOnce({ rows: [{ id: 'cust-1', email_verified: false }] });
    p.clientQuery
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-op' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(p.pool, {
      trialId: TRIAL,
      resolvedEmail: 'maya@example.com',
      source: 'admin',
      skipStartLimits: true,
    });
    expect(res.buildId).toBe('build-op');
    const sqls = [...p.allCalls, ...p.allClientCalls].map(([sql]) => sql);
    expect(sqls.some((sql) => /pg_advisory_xact_lock|build_start_events/.test(sql))).toBe(false);
  });

  it('keeper: refused at a limit before the extraction, and counted in the ledger when accepted', async () => {
    const busy = makePool({ budget: { active: 2, starts_today: 0 } });
    busy.query.mockResolvedValueOnce({ rows: [{ customer_id: KEEPER, handoff_build_id: null, handoff_at: null, email: 'keeper@example.com' }] });
    await expect(dispatchHarborKeeperBuild(busy.pool, { customerId: KEEPER })).rejects.toMatchObject({ code: 'build_limit' });
    expect(mockExtract).not.toHaveBeenCalled();
    expect(busy.connect).not.toHaveBeenCalled();

    const ok = makePool();
    ok.query.mockResolvedValueOnce({ rows: [{ customer_id: KEEPER, handoff_build_id: null, handoff_at: null, email: 'keeper@example.com' }] });
    ok.clientQuery
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-k' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    await dispatchHarborKeeperBuild(ok.pool, { customerId: KEEPER });
    const lock = ok.allClientCalls.find(([sql]) => /pg_advisory_xact_lock/.test(sql));
    expect(lock?.[1]).toEqual([KEEPER]);
    const ledger = ok.allClientCalls.find(([sql]) => /INSERT INTO build_start_events/.test(sql));
    expect(ledger?.[1]).toEqual([KEEPER, 'build-k', HARBOR_DISPATCH_START_KIND]);
  });
});

/** Each statement of the insert transaction, by what it does. */
function transactionSteps(calls: Array<[string, unknown[] | undefined]>): string[] {
  return calls.map(([sql]) =>
    sql === 'BEGIN' || sql === 'COMMIT' || sql === 'ROLLBACK'
      ? sql
      : /pg_advisory_xact_lock/.test(sql)
        ? 'customer-lock'
        : /FOR UPDATE/.test(sql)
          ? 'row-lock'
          : /FROM customers WHERE id = \$1 FOR SHARE/.test(sql)
            ? 'account-share-lock'
            : /INSERT INTO customers/.test(sql)
              ? 'create-account'
              : /FROM build_start_events/.test(sql)
                ? 'counts'
                : /INSERT INTO builds /.test(sql)
                  ? 'insert-build'
                  : /UPDATE trial_keepers/.test(sql)
                    ? 'stamp'
                    : /INSERT INTO build_start_events/.test(sql)
                      ? 'ledger'
                      : `other: ${sql}`
  );
}

describe('an anonymous dispatch attaches only to an account it creates, in the insert transaction', () => {
  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: [], costMicros: 0 });
    mockTranslate.mockReturnValue({
      spec: { app_name: 'Maya Reiki', tier: 'build', _seed_settings: { about_body: 'I teach breathwork.' } },
      warnings: [],
    });
    resetDailyCallBudget();
  });
  const anonymous = { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve', anonymous: true };

  it.each([
    ['an unverified account with no build and no keeper row', { id: 'cust-bare', email_verified: false }, 'address_in_use'],
    ['a verified account', { id: 'cust-owner', email_verified: true }, 'account_exists'],
  ] as const)('%s: refused before the extraction, and nothing is written', async (_label, known, code) => {
    const p = makePool({ knownAccount: known });
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    await expect(dispatchHarborTrialBuild(p.pool, anonymous)).rejects.toMatchObject({ code });
    expect(mockExtract).not.toHaveBeenCalled();
    expect(p.connect).not.toHaveBeenCalled();
    expect(p.allCalls.some(([sql]) => /INSERT INTO|UPDATE /.test(sql))).toBe(false);
  });

  it('a new address: the account row is created in the transaction that inserts the build, and the build records it', async () => {
    const p = makePool();
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] }) // row lock
      .mockResolvedValueOnce({ rows: [{ id: 'cust-new' }] }) // INSERT customers ... ON CONFLICT DO NOTHING RETURNING id
      .mockResolvedValueOnce({ rows: [{ id: 'build-new' }] }) // INSERT build
      .mockResolvedValueOnce({}) // stamp
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborTrialBuild(p.pool, anonymous);
    expect(res).toMatchObject({ buildId: 'build-new', customerId: 'cust-new', alreadyDispatched: false });
    // The row nobody else can see yet has no builds and no starts to count, so no lock and no counts; the start is recorded.
    expect(transactionSteps(p.allClientCalls)).toEqual(['BEGIN', 'row-lock', 'create-account', 'insert-build', 'stamp', 'ledger', 'COMMIT']);
    const create = p.allClientCalls.find(([sql]) => /INSERT INTO customers/.test(sql));
    expect(create?.[0]).toMatch(/ON CONFLICT \(email\) DO NOTHING\s+RETURNING id/);
    expect(create?.[1]?.[0]).toBe('maya@example.com');
    const insert = p.allClientCalls.find(([sql]) => /INSERT INTO builds /.test(sql));
    expect(insert?.[1]).toEqual(['cust-new', 'build', expect.any(String), 'Maya Reiki', true]);
    const ledger = p.allClientCalls.find(([sql]) => /INSERT INTO build_start_events/.test(sql));
    expect(ledger?.[1]).toEqual(['cust-new', 'build-new', HARBOR_DISPATCH_START_KIND]);
    // No account was written outside that transaction.
    expect(p.allCalls.some(([sql]) => /INSERT INTO customers/.test(sql))).toBe(false);
  });

  it('an address that gained an account while the model ran: the INSERT returns nothing, address_in_use, and the transaction is rolled back with no build', async () => {
    const p = makePool();
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] }) // row lock
      .mockResolvedValueOnce({ rows: [] }) // INSERT customers: the address is taken
      .mockResolvedValueOnce({}); // ROLLBACK
    await expect(dispatchHarborTrialBuild(p.pool, anonymous)).rejects.toMatchObject({ code: 'address_in_use' });
    expect(mockExtract).toHaveBeenCalledTimes(1);
    expect(transactionSteps(p.allClientCalls)).toEqual(['BEGIN', 'row-lock', 'create-account', 'ROLLBACK']);
    expect(p.client.release).toHaveBeenCalled();
  });

  it('a concurrent dispatch of the same conversation stamped it first: its build is returned and no account is made', async () => {
    const p = makePool();
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: 'winner-build', handoff_at: new Date() }] })
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborTrialBuild(p.pool, anonymous);
    expect(res).toMatchObject({ buildId: 'winner-build', alreadyDispatched: true });
    expect(transactionSteps(p.allClientCalls)).toEqual(['BEGIN', 'row-lock', 'COMMIT']);
  });
});

describe('a dispatch that may not attach to a verified account reads it again under a share lock', () => {
  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: [], costMicros: 0 });
    mockTranslate.mockReturnValue({ spec: { app_name: 'Maya Reiki', tier: 'build' }, warnings: [] });
  });

  it('an account verified while the model ran: account_exists, and no build', async () => {
    const p = makePool({ verifiedUnderLock: true });
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.query.mockResolvedValueOnce({ rows: [] }); // upsert: the address has an account
    p.query.mockResolvedValueOnce({ rows: [{ id: 'cust-1', email_verified: false }] }); // re-SELECT: not verified yet
    p.clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] }) // row lock
      .mockResolvedValueOnce({}); // ROLLBACK
    await expect(
      dispatchHarborTrialBuild(p.pool, { trialId: TRIAL, resolvedEmail: 'maya@example.com', source: 'self_serve' })
    ).rejects.toMatchObject({ code: 'account_exists' });
    expect(transactionSteps(p.allClientCalls)).toEqual(['BEGIN', 'customer-lock', 'row-lock', 'account-share-lock', 'ROLLBACK']);
    const share = p.allClientCalls.find(([sql]) => /FOR SHARE/.test(sql));
    expect(share?.[1]).toEqual(['cust-1']);
  });

  it('a caller that proved the account (allowVerifiedAccount) does not ask', async () => {
    const p = makePool({ verifiedUnderLock: true });
    p.query.mockResolvedValueOnce({ rows: [trialRow()] });
    p.query.mockResolvedValueOnce({ rows: [] });
    p.query.mockResolvedValueOnce({ rows: [{ id: 'cust-own', email_verified: true }] });
    p.clientQuery
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-own' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(p.pool, {
      trialId: TRIAL,
      resolvedEmail: 'own@example.com',
      source: 'self_serve',
      allowVerifiedAccount: true,
    });
    expect(res.buildId).toBe('build-own');
    expect(p.allClientCalls.some(([sql]) => /FOR SHARE/.test(sql))).toBe(false);
    const insert = p.allClientCalls.find(([sql]) => /INSERT INTO builds /.test(sql));
    expect(insert?.[1]?.[4]).toBe(false);
  });
});

// The worker writes the owning account's email into the spec as admin_email,
// and the build engine refuses anything but a plain address there. Signup and
// the conversation's email field accept more, so the dispatch has to ask.
describe('the address a build is owned by must be one the build engine accepts', () => {
  const KEEPER = 'c0ffee00-0000-4000-8000-000000000003';
  // An apostrophe, an underscore in the host, a letter outside ASCII, a local
  // part of 65 characters, and nine labels after the first.
  const REFUSED_BY_THE_ENGINE = [
    "o'brien@example.com",
    'maya@my_studio.example',
    `jos${String.fromCharCode(233)}@example.com`,
    `${'a'.repeat(65)}@example.com`,
    'maya@a.b.c.d.e.f.g.h.i.example',
  ];
  const ACCEPTED_BY_THE_ENGINE = ['maya@example.com', 'maya_b.c+site%1-x@sub-domain.example.org', 'MAYA@EXAMPLE.COM'];

  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: [], costMicros: 0 });
    mockTranslate.mockReturnValue({ spec: { app_name: 'Maya Reiki', tier: 'build' }, warnings: [] });
    resetDailyCallBudget();
  });

  it('isBuildableOwnerEmail is the engine rule, and nothing that is not a string passes it', () => {
    for (const address of REFUSED_BY_THE_ENGINE) expect(isBuildableOwnerEmail(address), address).toBe(false);
    for (const address of ACCEPTED_BY_THE_ENGINE) expect(isBuildableOwnerEmail(address), address).toBe(true);
    for (const missing of [null, undefined, '']) expect(isBuildableOwnerEmail(missing)).toBe(false);
    // Stored with a space, the worker would hand the engine the space too.
    expect(isBuildableOwnerEmail(' maya@example.com')).toBe(false);
  });

  it.each(REFUSED_BY_THE_ENGINE)('trial: %s is refused after the trial is read and before anything else', async (address) => {
    const { pool, query, connect, allCalls } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow({ email_captured: address })] });
    const before = anonymousCallsAdmitted();
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: address, source: 'self_serve', refuseBarrenSpec: true, anonymous: true })
    ).rejects.toMatchObject({ code: 'email_not_buildable' });
    // One statement ran: the trial load. No account lookup (so the answer
    // cannot depend on whether the address has an account), no budget read,
    // no customers row, no transaction.
    expect(allCalls).toHaveLength(1);
    expect(allCalls[0][0]).toMatch(/FROM trial_keepers/);
    expect(mockExtract).not.toHaveBeenCalled();
    expect(connect).not.toHaveBeenCalled();
    // And no slot of the anonymous share of the model-call budget was taken.
    expect(anonymousCallsAdmitted()).toBe(before);
  });

  it('trial: the refusal does not depend on the account behind the address (verified, unverified or none), nor on who asks', async () => {
    const address = "o'brien@example.com";
    for (const knownAccount of [null, { id: 'cust-1', email_verified: false }, { id: 'cust-1', email_verified: true }]) {
      for (const flags of [{}, { allowVerifiedAccount: true }, { allowVerifiedAccount: true, allowRedispatch: true, skipStartLimits: true }]) {
        const { pool, query, allCalls } = makePool({ knownAccount });
        query.mockResolvedValueOnce({ rows: [trialRow({ email_captured: address })] });
        await expect(
          dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: address, source: 'self_serve', ...flags })
        ).rejects.toMatchObject({ code: 'email_not_buildable' });
        expect(allCalls).toHaveLength(1);
      }
    }
    expect(mockExtract).not.toHaveBeenCalled();
  });

  it('trial: a build that is already attached is still returned, whatever the address', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow({ email_captured: "o'brien@example.com", handoff_build_id: 'existing' })] });
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: "o'brien@example.com", source: 'self_serve' });
    expect(res).toMatchObject({ buildId: 'existing', alreadyDispatched: true });
  });

  it.each(ACCEPTED_BY_THE_ENGINE)('trial: %s goes on to the extraction', async (address) => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow({ email_captured: address })] }); // trial load
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-new' }] }); // upsert
    clientQuery
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-ok' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: address, source: 'self_serve' });
    expect(res.buildId).toBe('build-ok');
    expect(mockExtract).toHaveBeenCalledTimes(1);
  });

  it.each([...REFUSED_BY_THE_ENGINE, null])('keeper: an account whose email is %s is refused before the limits and the extraction', async (address) => {
    const { pool, query, connect, allCalls } = makePool();
    query.mockResolvedValueOnce({ rows: [{ customer_id: KEEPER, handoff_build_id: null, handoff_at: null, email: address }] });
    const refusal = await dispatchHarborKeeperBuild(pool, { customerId: KEEPER, refuseBarrenSpec: true }).catch((err) => err);
    expect(refusal).toBeInstanceOf(HarborDispatchError);
    expect(refusal.code).toBe('email_not_buildable');
    // The owner is told in words they can act on; it is their own account.
    expect(refusal.message).toBe(OWNER_EMAIL_NOT_BUILDABLE_MESSAGE);
    expect(refusal.message).toMatch(/support@craftandship\.io/);
    expect(allCalls).toHaveLength(1);
    expect(allCalls[0][0]).toMatch(/FROM keepers k\s+JOIN customers c/);
    expect(mockExtract).not.toHaveBeenCalled();
    expect(connect).not.toHaveBeenCalled();
  });

  it('keeper: a build that is already attached is still returned, and a used conversation is still refused as used', async () => {
    const attached = makePool();
    attached.query.mockResolvedValueOnce({
      rows: [{ customer_id: KEEPER, handoff_build_id: 'existing', handoff_at: new Date(), email: "o'brien@example.com" }],
    });
    expect(await dispatchHarborKeeperBuild(attached.pool, { customerId: KEEPER })).toMatchObject({ buildId: 'existing', alreadyDispatched: true });

    const used = makePool();
    used.query.mockResolvedValueOnce({
      rows: [{ customer_id: KEEPER, handoff_build_id: null, handoff_at: new Date(), email: "o'brien@example.com" }],
    });
    await expect(dispatchHarborKeeperBuild(used.pool, { customerId: KEEPER })).rejects.toMatchObject({ code: 'build_already_used' });
  });
});

describe('self-serve content guard (R1-W2 review)', () => {
  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: [], costMicros: 0 });
  });

  it('refuses a spec with nothing from the keeper, and does not create a customer row', async () => {
    const { pool, query, connect } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] }); // trial load
    // No unmocked customer upsert result is queued: reaching it would throw.
    mockTranslate.mockReturnValueOnce({ spec: { app_name: 'unnamed', tier: 'build' }, warnings: [] });
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'victim@example.com', source: 'self_serve', refuseBarrenSpec: true })
    ).rejects.toMatchObject({ code: 'barren_spec' });
    expect(connect).not.toHaveBeenCalled();
    const sqls = query.mock.calls.map((c) => String(c[0]));
    expect(sqls.some((q) => /INSERT INTO customers/.test(q))).toBe(false);
  });

  it('a BARREN_SPEC refusal also leaves no customer row behind', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    mockTranslate.mockReturnValueOnce({ spec: { app_name: 'X' }, warnings: ['BARREN_SPEC: 0 of 8'] });
    await expect(
      dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'victim@example.com', source: 'self_serve', refuseBarrenSpec: true })
    ).rejects.toMatchObject({ code: 'barren_spec' });
    expect(query.mock.calls.some((c) => /INSERT INTO customers/.test(String(c[0])))).toBe(false);
  });

  it('operator dispatch (refuseBarrenSpec unset) still accepts a content-free spec', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [trialRow()] });
    query.mockResolvedValueOnce({ rows: [{ id: 'cust-1' }] });
    mockTranslate.mockReturnValueOnce({ spec: { app_name: 'X', tier: 'build' }, warnings: [] });
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-op' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborTrialBuild(pool, { trialId: TRIAL, resolvedEmail: 'a@example.test', source: 'admin' });
    expect(res.buildId).toBe('build-op');
  });

  describe('isContentFreeSpec', () => {
    it('is true for an empty spec and for the "Get in touch" placeholder alone', () => {
      expect(isContentFreeSpec({})).toBe(true);
      expect(isContentFreeSpec({ app_name: 'x', _seed_settings: {} })).toBe(true);
      expect(
        isContentFreeSpec({ _harbor_meta: { services: [{ title: 'Get in touch' }] } })
      ).toBe(true);
      // Contact details alone are not content.
      expect(isContentFreeSpec({ _seed_settings: { contact_email: 'a@example.test' } })).toBe(true);
    });

    it('is false as soon as the keeper supplied one real signal', () => {
      expect(isContentFreeSpec({ _seed_settings: { about_body: 'I teach breathwork.' } })).toBe(false);
      expect(isContentFreeSpec({ _seed_settings: { hero_tagline: 'Breathe' } })).toBe(false);
      expect(isContentFreeSpec({ _seed_settings: { site_name: 'Maya Reiki' } })).toBe(false);
      expect(isContentFreeSpec({ _harbor_meta: { services: [{ title: '1:1 Session' }] } })).toBe(false);
      expect(isContentFreeSpec({ _harbor_meta: { testimonials: [{ quote: 'Great' }] } })).toBe(false);
      expect(isContentFreeSpec({ _harbor_meta: { booking_intents: [{ label: 'Book' }] } })).toBe(false);
    });
  });
});

describe('dispatchHarborKeeperBuild (the keeper path of POST /lighthouse/build)', () => {
  const KEEPER = 'c0ffee00-0000-4000-8000-000000000002';
  const keeperRow = (over: Record<string, unknown> = {}) => ({
    customer_id: KEEPER,
    handoff_build_id: null,
    handoff_at: null,
    email: 'keeper@example.com',
    ...over,
  });

  beforeEach(() => {
    mockExtract.mockReset();
    mockTranslate.mockReset();
    mockExtract.mockResolvedValue({ spec: { pages: [] }, warnings: ['extraction: note'], costMicros: 0 });
    mockTranslate.mockReturnValue({
      spec: { app_name: 'Maya Reiki', tier: 'build', _seed_settings: { about_body: 'I teach breathwork.' } },
      warnings: [],
    });
  });

  it('throws keeper_not_found when the account has no keepers row', async () => {
    const { pool, query } = makePool();
    query.mockResolvedValueOnce({ rows: [] });
    await expect(dispatchHarborKeeperBuild(pool, { customerId: KEEPER })).rejects.toMatchObject({ code: 'keeper_not_found' });
    expect(mockExtract).not.toHaveBeenCalled();
  });

  it('is idempotent: returns the attached build without extracting', async () => {
    const { pool, query, connect } = makePool();
    query.mockResolvedValueOnce({ rows: [keeperRow({ handoff_build_id: 'existing', handoff_at: new Date() })] });
    const res = await dispatchHarborKeeperBuild(pool, { customerId: KEEPER });
    expect(res).toMatchObject({ buildId: 'existing', alreadyDispatched: true, customerId: KEEPER });
    expect(mockExtract).not.toHaveBeenCalled();
    expect(connect).not.toHaveBeenCalled();
  });

  it('goes through the same steps as a trial: keeper-keyed extraction, the shared Harbor insert, the keepers stamp', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [keeperRow()] });
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] }) // SELECT ... FOR UPDATE
      .mockResolvedValueOnce({ rows: [{ id: 'build-k' }] }) // INSERT build
      .mockResolvedValueOnce({}) // UPDATE keepers
      .mockResolvedValueOnce({}); // COMMIT
    const res = await dispatchHarborKeeperBuild(pool, { customerId: KEEPER, refuseBarrenSpec: true });

    expect(res).toMatchObject({ buildId: 'build-k', alreadyDispatched: false, customerId: KEEPER, tier: 'build' });
    expect(res.warnings).toContain('extraction: note');
    expect(mockExtract).toHaveBeenCalledWith({ pool, keeperCustomerId: KEEPER });
    // No customers row is created or looked up by address: the keeper already
    // is one. (The account's own email is read with the keeper row.)
    expect(query.mock.calls.some((c) => /INSERT INTO customers|FROM customers WHERE/.test(String(c[0])))).toBe(false);
    expect(query.mock.calls[0][0]).toMatch(/SELECT k\.customer_id, k\.handoff_build_id, k\.handoff_at, c\.email\s+FROM keepers k\s+JOIN customers c ON c\.id = k\.customer_id\s+WHERE k\.customer_id = \$1/);
    expect(clientQuery.mock.calls[1][0]).toMatch(/FROM keepers WHERE customer_id = \$1 FOR UPDATE/);
    const insert = clientQuery.mock.calls.find((c) => /INSERT INTO builds/.test(c[0]));
    expect(insert[0]).toMatch(/is_harbor, owner_created_by_dispatch\)\s+VALUES \(\$1, \$2, 'queued', \$3, \$4, TRUE, \$5\)/);
    expect(insert[1][0]).toBe(KEEPER);
    // Dispatch-time warnings are kept on the spec for the reviewer, as on the trial path.
    expect(JSON.parse(insert[1][2])._extraction_warnings).toEqual(['extraction: note']);
    const stamp = clientQuery.mock.calls.find((c) => /UPDATE keepers/.test(c[0]));
    expect(stamp[1]).toEqual(['build-k', KEEPER]);
  });

  it('coerces a tier the builds table would reject, and keeps the note on the spec', async () => {
    const { pool, query, clientQuery } = makePool();
    query.mockResolvedValueOnce({ rows: [keeperRow()] });
    mockTranslate.mockReturnValueOnce({
      spec: { app_name: 'X', tier: 'music-instructor', _seed_settings: { about_body: 'I teach piano.' } },
      warnings: [],
    });
    clientQuery
      .mockResolvedValueOnce({}) // BEGIN
      .mockResolvedValueOnce({ rows: [{ handoff_build_id: null, handoff_at: null }] })
      .mockResolvedValueOnce({ rows: [{ id: 'build-tier' }] })
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce({});
    const res = await dispatchHarborKeeperBuild(pool, { customerId: KEEPER, refuseBarrenSpec: true });
    expect(res.tier).toBe('build');
    expect(res.warnings.some((w) => /coerced to 'build'/.test(w))).toBe(true);
    const insert = clientQuery.mock.calls.find((c) => /INSERT INTO builds/.test(c[0]));
    expect(insert[1][1]).toBe('build');
    expect(JSON.parse(insert[1][2])._translation_warnings.some((w: string) => /coerced to 'build'/.test(w))).toBe(true);
  });

  it('refuses a keeper whose build was dispatched and has since been deleted', async () => {
    const { pool, query, connect } = makePool();
    query.mockResolvedValueOnce({ rows: [keeperRow({ handoff_at: new Date('2026-05-01T00:00:00Z') })] });
    await expect(dispatchHarborKeeperBuild(pool, { customerId: KEEPER })).rejects.toMatchObject({ code: 'build_already_used' });
    expect(mockExtract).not.toHaveBeenCalled();
    expect(connect).not.toHaveBeenCalled();
  });

  it('refuses a barren spec when asked to, and opens no transaction', async () => {
    const { pool, query, connect } = makePool();
    query.mockResolvedValueOnce({ rows: [keeperRow()] });
    mockTranslate.mockReturnValueOnce({ spec: { app_name: 'unnamed', tier: 'build' }, warnings: [] });
    await expect(
      dispatchHarborKeeperBuild(pool, { customerId: KEEPER, refuseBarrenSpec: true })
    ).rejects.toMatchObject({ code: 'barren_spec' });
    expect(connect).not.toHaveBeenCalled();
  });
});

describe('insertHarborBuild', () => {
  it('is the one INSERT in the service, and it sets is_harbor', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const source = fs.readFileSync(path.join(__dirname, '../../src/services/harbor_dispatch.ts'), 'utf-8');
    expect(source.match(/INSERT INTO builds/g)).toHaveLength(1);

    const query = vi.fn().mockResolvedValue({ rows: [{ id: 'build-x' }] });
    const id = await insertHarborBuild({ query } as never, {
      customerId: 'cust-1',
      tier: 'build',
      spec: { app_name: 'x' },
      appName: 'x',
    });
    expect(id).toBe('build-x');
    expect(query.mock.calls[0][0]).toMatch(/INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)\s+VALUES \(\$1, \$2, 'queued', \$3, \$4, TRUE, \$5\)/);
    expect(query.mock.calls[0][1]).toEqual(['cust-1', 'build', '{"app_name":"x"}', 'x', false]);
  });

  it('no other web route inserts a Harbor root build by its own statement', async () => {
    // lighthouse.ts and admin.ts both dispatch; neither carries an INSERT INTO
    // builds for it any more (admin.ts keeps one, for "fix as new", which sets
    // parent_build_id and so inherits the flag from the trigger).
    const fs = await import('fs');
    const path = await import('path');
    const read = (file: string) => fs.readFileSync(path.join(__dirname, '../../src/routes', file), 'utf-8');
    expect(read('lighthouse.ts')).not.toMatch(/INSERT INTO builds/);
    const adminInserts = read('admin.ts').match(/INSERT INTO builds[\s\S]{0,160}/g) || [];
    expect(adminInserts).toHaveLength(1);
    expect(adminInserts[0]).toMatch(/parent_build_id/);
  });
});

describe('HarborDispatchError', () => {
  it('carries a typed code', () => {
    const e = new HarborDispatchError('no_email', 'nope');
    expect(e.code).toBe('no_email');
    expect(e).toBeInstanceOf(Error);
  });
});
