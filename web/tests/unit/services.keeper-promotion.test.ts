import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Pool, PoolClient } from 'pg';
import {
  promoteTrialToKeeper,
  promoteTrialInTransaction,
  claimKeeperSubscription,
  PAID_TRIAL_ACCESS_DAYS,
  PUBLISHING_SUBSCRIPTION_STATUSES,
} from '../../src/services/keeper_promotion';

// Helper: build a mock Pool whose connect() returns a mock PoolClient with
// a programmable query queue. Each test queues responses in order; the
// service's transaction sequence is deterministic so we can assert exactly
// which SQL fired with which params.

interface MockClientQueue {
  queue: Array<{ rows: unknown[]; rowCount?: number } | Error>;
  query: ReturnType<typeof vi.fn>;
  release: ReturnType<typeof vi.fn>;
}

function makeMockClient(): MockClientQueue {
  const queue: Array<{ rows: unknown[]; rowCount?: number } | Error> = [];
  const query = vi.fn(() => {
    const next = queue.shift();
    if (next === undefined) {
      throw new Error('mock client query: queue exhausted; test misconfigured');
    }
    if (next instanceof Error) {
      return Promise.reject(next);
    }
    return Promise.resolve(next);
  });
  return { queue, query, release: vi.fn() };
}

function makePoolWithClient(client: MockClientQueue): Pool {
  return {
    connect: vi.fn().mockResolvedValue(client as unknown as PoolClient),
  } as unknown as Pool;
}

const TRIAL_ID = '11111111-2222-3333-4444-555555555555';
const CUSTOMER_ID = '99999999-8888-7777-6666-555555555555';

/** What the keeper claim answers when it wrote the row. */
const CLAIMED = { rows: [{ customer_id: CUSTOMER_ID }] };

const VALID_OPTS = {
  trialId: TRIAL_ID,
  customerId: CUSTOMER_ID,
  email: 'river@example.com',
  stripeSubscriptionId: 'sub_test_123',
  stripeCustomerId: 'cus_test_123',
  monthlyAmountCents: 2900,
  slidingScaleAttestation: false,
};

describe('promoteTrialToKeeper input validation', () => {
  it('throws when trialId is empty', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    await expect(
      promoteTrialToKeeper({ ...VALID_OPTS, trialId: '', pool })
    ).rejects.toThrow(/trialId is required/);
  });

  it('throws when customerId is empty', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    await expect(
      promoteTrialToKeeper({ ...VALID_OPTS, customerId: '', pool })
    ).rejects.toThrow(/customerId is required/);
  });

  it('throws when stripeSubscriptionId is empty', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    await expect(
      promoteTrialToKeeper({ ...VALID_OPTS, stripeSubscriptionId: '', pool })
    ).rejects.toThrow(/stripeSubscriptionId is required/);
  });

  it('throws when monthlyAmountCents is negative', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    await expect(
      promoteTrialToKeeper({ ...VALID_OPTS, monthlyAmountCents: -100, pool })
    ).rejects.toThrow(/non-negative/);
  });

  it("throws when stripeCustomerId doesn't start with cus_", async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    await expect(
      promoteTrialToKeeper({ ...VALID_OPTS, stripeCustomerId: 'wrong_123', pool })
    ).rejects.toThrow(/stripeCustomerId should start/);
  });

  it("throws when stripeSubscriptionId doesn't start with sub_", async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    await expect(
      promoteTrialToKeeper({
        ...VALID_OPTS,
        stripeSubscriptionId: 'invalid_id',
        pool,
      })
    ).rejects.toThrow(/stripeSubscriptionId should start/);
  });
});

describe('promoteTrialToKeeper happy path', () => {
  let client: MockClientQueue;
  let pool: Pool;

  beforeEach(() => {
    client = makeMockClient();
    pool = makePoolWithClient(client);
  });

  it('runs the full transaction sequence and returns counts', async () => {
    // 1: BEGIN, 2: SELECT trial FOR UPDATE, 3: INSERT keepers (the claim),
    // 4: SELECT existing harbor_subscriptions, 5: INSERT harbor_subscriptions,
    // 6: UPDATE companion_turns, 7: UPDATE keeper_memory,
    // 8: UPDATE trial_keepers (released_at), 9: COMMIT
    client.queue.push({ rows: [] });                                                    // BEGIN
    client.queue.push({
      rows: [
        {
          trial_id: TRIAL_ID,
          companion_name: 'Haven',
          companion_assigned: 'Haven',
          released_at: null,
        },
      ],
    });                                                                                  // SELECT trial
    client.queue.push({ rows: [{ customer_id: CUSTOMER_ID }] });                        // INSERT keepers (claimed)
    client.queue.push({ rows: [] });                                                    // SELECT existing sub
    client.queue.push({ rows: [] });                                                    // INSERT harbor_subscriptions
    client.queue.push({ rows: [{ turn_id: 't1' }, { turn_id: 't2' }], rowCount: 2 });   // UPDATE companion_turns
    client.queue.push({ rows: [{ memory_id: 'm1' }, { memory_id: 'm2' }, { memory_id: 'm3' }], rowCount: 3 }); // UPDATE keeper_memory
    client.queue.push({ rows: [] });                                                    // UPDATE trial_keepers
    client.queue.push({ rows: [] });                                                    // COMMIT

    const result = await promoteTrialToKeeper({ ...VALID_OPTS, pool });

    expect(result.promoted).toBe(true);
    expect(result.customerId).toBe(CUSTOMER_ID);
    expect(result.conversationTurnsTransferred).toBe(2);
    expect(result.memoryRowsTransferred).toBe(3);
    expect(client.release).toHaveBeenCalledOnce();
  });

  it('uses FOR UPDATE on the trial row to prevent concurrent promotion', async () => {
    client.queue.push({ rows: [] }); // BEGIN
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    // Fail fast after the trial select to inspect SQL.
    client.queue.push(new Error('test stop'));

    await expect(promoteTrialToKeeper({ ...VALID_OPTS, pool })).rejects.toThrow();
    const trialSelectSql = client.query.mock.calls[1][0] as string;
    expect(trialSelectSql).toMatch(/SELECT trial_id, companion_name/);
    expect(trialSelectSql).toMatch(/FOR UPDATE/);
  });

  it('persists sliding-scale attestation + amount when set', async () => {
    client.queue.push({ rows: [] });
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push(CLAIMED); // INSERT keepers (the claim)
    client.queue.push({ rows: [] }); // SELECT existing sub
    client.queue.push({ rows: [] }); // INSERT harbor_subscriptions
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [] });
    client.queue.push({ rows: [] }); // COMMIT

    await promoteTrialToKeeper({
      ...VALID_OPTS,
      pool,
      slidingScaleAttestation: true,
      monthlyAmountCents: 900, // $9 sliding-scale floor
    });

    // The keeper row is written by the claim (call index 2).
    const claimCall = client.query.mock.calls[2];
    const claimSql = claimCall[0] as string;
    const claimParams = claimCall[1] as unknown[];
    expect(claimSql).toMatch(/INSERT INTO keepers/);
    expect(claimSql).toMatch(/sliding_scale_attestation/);
    expect(claimParams[6]).toBe(true);               // sliding_scale_attestation
    expect(claimParams[7]).toBe(900);                // sliding_scale_amount_cents
  });

  it('persists monthlyAmountCents on default-payers (attestation=false)', async () => {
    // Regression guard for `bug-keepers-sliding-amount-null-on-default-payers`
    // (2026-04-30): default-$39 payers (the majority) need their monthly
    // amount recorded on the keeper row so the lamp live-state can render
    // "$X a month". Pre-fix code stored NULL when attestation=false; post-fix
    // always stores the chosen amount, attestation logged separately on its
    // own column.
    client.queue.push({ rows: [] });
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push(CLAIMED); // INSERT keepers (the claim)
    client.queue.push({ rows: [] }); // SELECT existing sub
    client.queue.push({ rows: [] }); // INSERT harbor_subscriptions
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [] });
    client.queue.push({ rows: [] }); // COMMIT

    await promoteTrialToKeeper({
      ...VALID_OPTS,
      pool,
      slidingScaleAttestation: false,
      monthlyAmountCents: 3900, // default $39
    });

    const claimParams = client.query.mock.calls[2][1] as unknown[];
    expect(claimParams[6]).toBe(false);               // attestation=false (default payer)
    expect(claimParams[7]).toBe(3900);                // amount STILL stored (was null pre-fix)
  });

  it('passes initialStatus into the harbor_subscriptions INSERT', async () => {
    client.queue.push({ rows: [] });
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push(CLAIMED); // INSERT keepers (the claim)
    client.queue.push({ rows: [] }); // SELECT existing sub
    client.queue.push({ rows: [] }); // INSERT harbor_subscriptions
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [] });
    client.queue.push({ rows: [] }); // COMMIT

    await promoteTrialToKeeper({ ...VALID_OPTS, pool, initialStatus: 'trialing' });

    // INSERT harbor_subscriptions is call index 4.
    const insertSubCall = client.query.mock.calls[4];
    const insertParams = insertSubCall[1] as unknown[];
    expect(insertParams[3]).toBe('trialing');
  });
});

describe('promoteTrialToKeeper idempotency', () => {
  it('returns promoted=false when the trial is already released', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    client.queue.push({ rows: [] }); // BEGIN
    client.queue.push({
      rows: [
        {
          trial_id: TRIAL_ID,
          companion_name: 'Haven',
          companion_assigned: 'Haven',
          released_at: new Date('2026-04-26T10:00:00Z'),
        },
      ],
    });
    client.queue.push({ rows: [{ customer_id: CUSTOMER_ID }] }); // SELECT existing keeper
    client.queue.push({ rows: [] }); // COMMIT

    const result = await promoteTrialToKeeper({ ...VALID_OPTS, pool });
    expect(result.promoted).toBe(false);
    expect(result.customerId).toBe(CUSTOMER_ID);
    expect(result.conversationTurnsTransferred).toBe(0);
    expect(result.memoryRowsTransferred).toBe(0);
  });

  it('throws on missing trial and rolls back', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    client.queue.push({ rows: [] }); // BEGIN
    client.queue.push({ rows: [] }); // SELECT trial: NO ROWS
    client.queue.push({ rows: [] }); // ROLLBACK (from catch)

    await expect(promoteTrialToKeeper({ ...VALID_OPTS, pool })).rejects.toThrow(
      /no trial_keeper/
    );
    const calls = client.query.mock.calls.map((c) => c[0]);
    expect(calls).toContain('ROLLBACK');
    expect(client.release).toHaveBeenCalledOnce();
  });

  it('skips the harbor_subscriptions INSERT when an existing audit row matches', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    client.queue.push({ rows: [] });
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push(CLAIMED); // INSERT keepers (the claim)
    client.queue.push({ rows: [{ id: 'existing-sub-uuid' }] }); // SELECT existing sub returns row
    // Note: NO INSERT harbor_subscriptions call should fire here.
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [] });
    client.queue.push({ rows: [] }); // COMMIT

    const result = await promoteTrialToKeeper({ ...VALID_OPTS, pool });
    expect(result.promoted).toBe(true);
    // 8 query calls total when INSERT is skipped (vs 9 in happy path).
    expect(client.query).toHaveBeenCalledTimes(8);
  });
});

describe('promoteTrialToKeeper error handling', () => {
  it('rolls back and rethrows when the conversation transfer fails', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    client.queue.push({ rows: [] });
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push(CLAIMED); // INSERT keepers (the claim)
    client.queue.push({ rows: [] }); // SELECT existing sub
    client.queue.push({ rows: [] }); // INSERT harbor_subscriptions
    client.queue.push(new Error('FK violation on companion_turns'));
    client.queue.push({ rows: [] }); // ROLLBACK

    await expect(promoteTrialToKeeper({ ...VALID_OPTS, pool })).rejects.toThrow(
      /FK violation/
    );
    const calls = client.query.mock.calls.map((c) => c[0]);
    expect(calls).toContain('ROLLBACK');
    expect(client.release).toHaveBeenCalledOnce();
  });

  it('a failing ROLLBACK does not mask the original error (R1-W2 review)', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    client.queue.push({ rows: [] }); // BEGIN
    client.queue.push(new Error('deadlock detected')); // SELECT trial FOR UPDATE fails
    client.queue.push(new Error('connection terminated')); // ROLLBACK also fails

    await expect(promoteTrialToKeeper({ ...VALID_OPTS, pool })).rejects.toThrow(
      /deadlock detected/
    );
    expect(client.release).toHaveBeenCalledOnce();
    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('ROLLBACK failed'),
      'connection terminated'
    );
    errSpy.mockRestore();
  });

  it('locks the trial row FOR UPDATE before touching keepers (concurrent webhook deliveries)', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    client.queue.push({ rows: [] }); // BEGIN
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: new Date() }],
    });
    client.queue.push({ rows: [{ customer_id: CUSTOMER_ID }] }); // existing keeper lookup
    client.queue.push({ rows: [] }); // COMMIT

    const result = await promoteTrialToKeeper({ ...VALID_OPTS, pool });
    expect(result.promoted).toBe(false);
    const sqls = client.query.mock.calls.map((c) => String(c[0]));
    expect(sqls[1]).toMatch(/FROM trial_keepers[\s\S]*FOR UPDATE/);
    // A duplicate delivery performs no writes.
    expect(sqls.some((q) => /INSERT INTO|UPDATE /.test(q))).toBe(false);
  });

  it('always releases the pg client even on error', async () => {
    const client = makeMockClient();
    const pool = makePoolWithClient(client);
    client.queue.push(new Error('connection lost')); // BEGIN fails
    client.queue.push({ rows: [] }); // ROLLBACK still attempted

    await expect(promoteTrialToKeeper({ ...VALID_OPTS, pool })).rejects.toThrow(
      /connection lost/
    );
    expect(client.release).toHaveBeenCalledOnce();
  });
});


describe('promotion carries the trial\'s build to the keeper and keeps the paying cookie recognizable', () => {
  const BUILD_ID = 'bbbbbbbb-1111-2222-3333-444444444444';
  const HANDOFF_AT = new Date('2026-04-25T09:00:00Z');

  function queueHappyPath(client: MockClientQueue): void {
    client.queue.push({ rows: [] }); // BEGIN
    client.queue.push({
      rows: [{
        trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null,
        handoff_build_id: BUILD_ID, handoff_at: HANDOFF_AT,
      }],
    });
    client.queue.push(CLAIMED); // INSERT keepers (the claim)
    client.queue.push({ rows: [] }); // SELECT existing sub
    client.queue.push({ rows: [] }); // INSERT harbor_subscriptions
    client.queue.push({ rows: [], rowCount: 0 }); // turns
    client.queue.push({ rows: [], rowCount: 0 }); // memory
    client.queue.push({ rows: [] }); // UPDATE trial_keepers
    client.queue.push({ rows: [] }); // COMMIT
  }

  it('copies handoff_build_id and handoff_at onto the keeper row without overwriting one that is set', async () => {
    const client = makeMockClient();
    queueHappyPath(client);
    await promoteTrialToKeeper({ ...VALID_OPTS, pool: makePoolWithClient(client) });

    const [sql, params] = client.query.mock.calls[2] as [string, unknown[]];
    expect(sql).toMatch(/INSERT INTO keepers/);
    expect(sql).toMatch(/handoff_build_id = COALESCE\(keepers\.handoff_build_id, EXCLUDED\.handoff_build_id\)/);
    expect(sql).toMatch(/handoff_at = COALESCE\(keepers\.handoff_at, EXCLUDED\.handoff_at\)/);
    expect(params[0]).toBe(CUSTOMER_ID);
    expect(params[8]).toBe(BUILD_ID);
    expect(params[9]).toBe(HANDOFF_AT);
  });

  it('releases the trial and moves its expiry out so the paying browser is not locked out', async () => {
    expect(PAID_TRIAL_ACCESS_DAYS).toBe(30);
    const client = makeMockClient();
    queueHappyPath(client);
    await promoteTrialToKeeper({ ...VALID_OPTS, pool: makePoolWithClient(client) });

    const [sql, params] = client.query.mock.calls[7] as [string, unknown[]];
    expect(sql).toMatch(/UPDATE trial_keepers/);
    expect(sql).toMatch(/released_at = NOW\(\)/);
    // The release records which account the trial went to: the cookie rule reads it back.
    expect(sql).toMatch(/promoted_customer_id = \$2/);
    expect(sql).toMatch(/expires_at = GREATEST\(expires_at, NOW\(\) \+ INTERVAL '30 days'\)/);
    expect(sql).toMatch(/WHERE trial_id = \$1/);
    expect(params).toEqual([TRIAL_ID, CUSTOMER_ID]);
  });
});

describe('claimKeeperSubscription: one statement decides whether the keeper row may be written', () => {
  const CLAIM = {
    customerId: CUSTOMER_ID,
    companionName: 'Haven',
    companionAssigned: 'Haven',
    stripeSubscriptionId: 'sub_test_123',
    stripeCustomerId: 'cus_test_123',
    status: 'active',
    slidingScaleAttestation: false,
    monthlyAmountCents: 2900,
    handoffBuildId: null,
    handoffAt: null,
    replaceEnded: false,
  };

  it('writes only when the row has no subscription, has this one, or (replaceEnded) has one that no longer publishes', async () => {
    const client = makeMockClient();
    client.queue.push({ rows: [{ customer_id: CUSTOMER_ID }] });
    expect(await claimKeeperSubscription(client as unknown as PoolClient, { ...CLAIM, replaceEnded: true })).toBe(true);

    const [sql, params] = client.query.mock.calls[0] as [string, unknown[]];
    // The condition is in the statement that writes, not in a read before it.
    expect(sql).toMatch(
      /ON CONFLICT \(customer_id\) DO UPDATE[\s\S]*WHERE keepers\.stripe_subscription_id IS NULL\s+OR keepers\.stripe_subscription_id = EXCLUDED\.stripe_subscription_id\s+OR \(\$11::boolean AND keepers\.subscription_status <> ALL\(\$12::text\[\]\)\)\s+RETURNING customer_id/
    );
    expect(params[10]).toBe(true);
    // The statuses that publish are the application's one list, not a copy.
    expect(params[11]).toBe(PUBLISHING_SUBSCRIPTION_STATUSES);
  });

  it('says false, having written nothing, when the statement matched no row', async () => {
    const client = makeMockClient();
    client.queue.push({ rows: [] });
    expect(await claimKeeperSubscription(client as unknown as PoolClient, CLAIM)).toBe(false);
    expect(client.query).toHaveBeenCalledTimes(1);
  });
});

describe('promoteTrialInTransaction (runs inside the caller\'s transaction)', () => {
  it('never issues BEGIN, COMMIT or ROLLBACK and never releases the client', async () => {
    const client = makeMockClient();
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push(CLAIMED); // INSERT keepers (the claim)
    client.queue.push({ rows: [] }); // SELECT existing sub
    client.queue.push({ rows: [] }); // INSERT harbor_subscriptions
    client.queue.push({ rows: [{ turn_id: 't1' }], rowCount: 1 });
    client.queue.push({ rows: [], rowCount: 0 });
    client.queue.push({ rows: [] }); // UPDATE trial_keepers

    const result = await promoteTrialInTransaction(client as unknown as PoolClient, VALID_OPTS);

    expect(result).toMatchObject({ promoted: true, trialFound: true, conversationTurnsTransferred: 1 });
    const sqls = client.query.mock.calls.map((c) => String(c[0]));
    expect(sqls.some((q) => /^\s*(BEGIN|COMMIT|ROLLBACK)\b/i.test(q))).toBe(false);
    expect(sqls[0]).toMatch(/FROM trial_keepers[\s\S]*FOR UPDATE/);
    expect(client.release).not.toHaveBeenCalled();
  });

  it('a keeper that already has a subscription of its own refuses the trial, and nothing is written after the claim', async () => {
    const client = makeMockClient();
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push({ rows: [] }); // the claim wrote nothing: the keeper belongs to another subscription

    const result = await promoteTrialInTransaction(client as unknown as PoolClient, VALID_OPTS);

    expect(result).toMatchObject({ promoted: false, refusal: 'account_has_subscription', trialFound: true });
    // The trial select and the claim; no audit row, no turns, no release.
    expect(client.query).toHaveBeenCalledTimes(2);
    const [sql, params] = client.query.mock.calls[1] as [string, unknown[]];
    expect(sql).toMatch(/INSERT INTO keepers/);
    // A trial replaces nothing: replaceEnded is false whatever the status of the other subscription.
    expect(params[10]).toBe(false);
  });

  it('a trial that is not the one stamped on the paid build is refused before anything is written', async () => {
    const client = makeMockClient();
    const stamped = 'bbbbbbbb-1111-2222-3333-444444444444';
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null, handoff_build_id: stamped }],
    });

    const named = 'cccccccc-1111-2222-3333-444444444444';
    const result = await promoteTrialInTransaction(client as unknown as PoolClient, { ...VALID_OPTS, buildId: named });
    expect(result).toMatchObject({ promoted: false, refusal: 'trial_not_for_build', trialFound: true });
    expect(client.query).toHaveBeenCalledTimes(1);

    // A trial with no build stamped on it is not the trial of any build.
    const unstamped = makeMockClient();
    unstamped.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null, handoff_build_id: null }],
    });
    expect(await promoteTrialInTransaction(unstamped as unknown as PoolClient, { ...VALID_OPTS, buildId: named })).toMatchObject({
      refusal: 'trial_not_for_build',
    });
  });

  it('the build match ignores the case of the id, and is skipped when no build is named (the development bypass)', async () => {
    const stamped = 'bbbbbbbb-1111-2222-3333-444444444444';
    for (const buildId of [stamped.toUpperCase(), undefined]) {
      const client = makeMockClient();
      client.queue.push({
        rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null, handoff_build_id: stamped }],
      });
      client.queue.push(CLAIMED);
      client.queue.push({ rows: [] }); // SELECT existing sub
      client.queue.push({ rows: [] }); // INSERT harbor_subscriptions
      client.queue.push({ rows: [], rowCount: 0 });
      client.queue.push({ rows: [], rowCount: 0 });
      client.queue.push({ rows: [] }); // release
      const result = await promoteTrialInTransaction(client as unknown as PoolClient, { ...VALID_OPTS, buildId });
      expect(result).toMatchObject({ promoted: true, refusal: null });
    }
  });

  it('reports a missing trial instead of throwing, and writes nothing', async () => {
    const client = makeMockClient();
    client.queue.push({ rows: [] }); // SELECT trial: none
    const result = await promoteTrialInTransaction(client as unknown as PoolClient, VALID_OPTS);
    expect(result).toMatchObject({ promoted: false, trialFound: false });
    expect(client.query).toHaveBeenCalledTimes(1);
  });

  it('lets a database error propagate so the caller\'s transaction rolls back', async () => {
    const client = makeMockClient();
    client.queue.push({
      rows: [{ trial_id: TRIAL_ID, companion_name: 'Haven', companion_assigned: 'Haven', released_at: null }],
    });
    client.queue.push(new Error('insert failed'));
    await expect(promoteTrialInTransaction(client as unknown as PoolClient, VALID_OPTS)).rejects.toThrow(/insert failed/);
  });
});
