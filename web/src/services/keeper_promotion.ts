/**
 * Trial -> keeper atomic promotion.
 *
 * When a cookie-trial keeper completes Stripe Checkout for the harbor
 * subscription, we transfer their conversation history + memory rows
 * to a permanent `keepers` row paired with `customers(id)`, in a single
 * transaction so a partial failure leaves no orphans.
 *
 * Phase 2 Slice 2A. See `docs/CAS_HARBOR_PHASE_2_TECH_PLAN.md` § 5.1.
 *
 * Two entry points:
 *   - `promoteTrialInTransaction(client, opts)` runs the steps on a client
 *     that is ALREADY inside a transaction and never issues BEGIN, COMMIT or
 *     ROLLBACK. The Stripe `checkout.session.completed` handler in
 *     `web/src/routes/webhooks.ts` uses it so that the event claim, the
 *     promotion and the build going live commit or roll back together.
 *   - `promoteTrialToKeeper(opts)` opens its own transaction around the same
 *     steps. The development payment bypass (`POST /lighthouse/bypass`) uses
 *     it.
 *
 * Idempotency: a trial that is already released is reported as
 * `promoted: false` and nothing is written. Concurrent promotions of the same
 * trial are serialized by a `FOR UPDATE` lock on the trial row.
 *
 * Which account a trial may be promoted into: only one whose keeper has no
 * subscription of its own. Two trials can hold builds on one unverified
 * account (both gave its address), and the conversation a keeper row holds is
 * the first payer's; a second trial promoted into it would be handed that
 * conversation. The rule is enforced here, in the statement that claims the
 * keeper row (`claimKeeperSubscription`), because the route that opens
 * Checkout (routes/lighthouse.ts) can only check it when the session is
 * opened, and a session opened earlier completes later, through the webhook.
 * A promotion that fails the rule writes nothing and says why
 * (`refusal`); the caller decides what becomes of the payment. A trial is also
 * promoted only for the build stamped on it (`buildId`), so that the release
 * below records a fact the cookie rule can check.
 *
 * What the trial cookie is worth afterwards: promotion sets `released_at`, so
 * the cookie stops being a trial identity. It is not thrown away, though: the
 * trial-cookie middleware keeps recognizing it as the browser that paid for
 * the trial's build, until `expires_at`, and only while `promoted_customer_id`
 * (written by the same statement that releases the trial) is the account of
 * that build and that account has a keeper row. Promotion therefore moves
 * `expires_at` out to PAID_TRIAL_ACCESS_DAYS from now, so that paying late in
 * the 7-day trial does not lock the payer out minutes later.
 */

import type { Pool, PoolClient } from 'pg';

// ---------------------------------------------------------------------------
// What a keeper's subscription status means
// ---------------------------------------------------------------------------
//
// keepers.subscription_status is written here (at promotion) and by the Stripe
// webhook handlers (routes/webhooks.ts), and read wherever the right to
// publish is checked (routes/builds.ts, routes/lighthouse.ts). The vocabulary
// and the rule are defined once, in this module, so that the writers and the
// readers cannot drift apart.

/**
 * The subscription statuses Stripe reports. keepers.subscription_status
 * stores them word for word (its CHECK constraint in db/schema.sql lists the
 * same eight), so the keeper row never claims more than Stripe does.
 */
export const STRIPE_SUBSCRIPTION_STATUSES: readonly string[] = [
  'active',
  'trialing',
  'past_due',
  'unpaid',
  'paused',
  'incomplete',
  'incomplete_expired',
  'canceled',
];

/**
 * The statuses in which a customer currently has the right to publish:
 *   - `active` and `trialing`: the subscription is in force.
 *   - `past_due`: the latest invoice failed and Stripe is still retrying it.
 *     The customer keeps the right to publish during that retry period (the
 *     lamp page asks them to update their card).
 * Every other status is refused. `incomplete` and `incomplete_expired` never
 * had a successful first payment; `canceled` and `incomplete_expired` are
 * final; `unpaid` is what a subscription becomes when the retries are
 * exhausted, the point at which Stripe's own guidance is to revoke access;
 * `paused` generates no invoices.
 */
export const PUBLISHING_SUBSCRIPTION_STATUSES: readonly string[] = ['active', 'trialing', 'past_due'];

/** The same list as a SQL literal list, for an IN (...) in statement text. The values are the constants above, never input. */
export const PUBLISHING_SUBSCRIPTION_STATUS_LIST_SQL = PUBLISHING_SUBSCRIPTION_STATUSES.map(
  (status) => `'${status}'`
).join(', ');

/**
 * The value for keepers.subscription_status given a Stripe status: the status
 * itself. A status this code does not know (a later Stripe API version, a
 * malformed event) is recorded as `incomplete`, which never publishes.
 */
export function keeperSubscriptionStatus(stripeStatus: string): string {
  return STRIPE_SUBSCRIPTION_STATUSES.includes(stripeStatus) ? stripeStatus : 'incomplete';
}

/**
 * How long after payment the paying browser's trial cookie is still accepted
 * as the owner of the build it paid for (see middleware/trial-cookie.ts).
 * After that the account, reached through the mailbox, is the way in.
 */
export const PAID_TRIAL_ACCESS_DAYS = 30;

export interface PromoteTrialOpts {
  pool: Pool;
  /** trial_keepers.trial_id (UUID). Must be active (released_at IS NULL). */
  trialId: string;
  /** customers.id (UUID). Caller resolved (created or matched by email). */
  customerId: string;
  /** customers.email (used for first-keeper companion seed). */
  email: string;
  /** Stripe subscription id (sub_...). Unique key in harbor_subscriptions. */
  stripeSubscriptionId: string;
  /** Stripe customer id (cus_...). */
  stripeCustomerId: string;
  /** Monthly amount in cents. Harbor default 3900 ($39.00); sliding scale floor 1900 ($19.00). */
  monthlyAmountCents: number;
  /** True if the keeper attested to using the sliding scale. */
  slidingScaleAttestation: boolean;
  /** Initial Stripe subscription status (active|trialing|incomplete). */
  initialStatus?: 'active' | 'trialing' | 'incomplete';
  /** Stripe period start/end as Date objects (from session/subscription). */
  currentPeriodStart?: Date;
  currentPeriodEnd?: Date;
}

export interface PromoteTrialStepOpts extends Omit<PromoteTrialOpts, 'pool'> {
  /**
   * The build this payment is for. When given, the trial must be the one
   * stamped on that build (`trial_keepers.handoff_build_id`); a trial that is
   * not is refused (`trial_not_for_build`). The development bypass pays for no
   * build and leaves it out.
   */
  buildId?: string;
}

/**
 * Why a payment was not applied to the account it names: the account's keeper
 * already carries a subscription of its own that this one may not replace, or
 * the trial the payment names is not the one stamped on its build.
 */
export type PaymentRefusal = 'account_has_subscription' | 'trial_not_for_build';

/**
 * What `claimKeeperSubscription` writes to a keeper row. `replaceEnded` is what
 * separates the two kinds of payment: a payment WITH a trial to promote
 * replaces no subscription at all, and a payment without one (a keeper paying
 * for another website) may replace a subscription that no longer publishes. A
 * keeper whose subscription still publishes does not need a second one (the
 * make-public route sends them to publish instead), so a second payment is
 * not applied to it.
 */
export interface KeeperClaim {
  customerId: string;
  companionName: string;
  companionAssigned: string;
  stripeSubscriptionId: string;
  stripeCustomerId: string;
  status: string;
  slidingScaleAttestation: boolean;
  monthlyAmountCents: number;
  /** The build to record on a keeper row that has none yet (a trial's build), or null. */
  handoffBuildId: string | null;
  handoffAt: Date | null;
  /** Whether a subscription whose status is not in PUBLISHING_SUBSCRIPTION_STATUSES may be replaced. False for a trial's payment. */
  replaceEnded: boolean;
}

/**
 * Point the account's keeper row at a subscription, creating the row when the
 * account has none, in ONE statement whose condition is evaluated on the row
 * as the statement finds it (after any concurrent writer has committed, in
 * READ COMMITTED): the row is written only when it has no subscription yet,
 * already has this one, or (`replaceEnded`) has one that no longer publishes.
 * Returns false, having written nothing, when the keeper belongs to another
 * subscription.
 *
 * A check made before this statement could be separated from it by a second
 * webhook for the same account; the condition lives in the statement so that
 * two sessions completing at once cannot both be applied. The row lock this
 * statement takes is taken after the trial's, the order every writer of these
 * rows follows (the webhook locks the build, then the trial, then this row).
 */
export async function claimKeeperSubscription(
  client: Pick<PoolClient, 'query'>,
  claim: KeeperClaim
): Promise<boolean> {
  const res = await client.query<{ customer_id: string }>(
    `INSERT INTO keepers
       (customer_id, companion_name, companion_assigned, subscription_status,
        stripe_subscription_id, stripe_customer_id, sliding_scale_attestation,
        sliding_scale_amount_cents, handoff_build_id, handoff_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
     ON CONFLICT (customer_id) DO UPDATE
       SET subscription_status = EXCLUDED.subscription_status,
           stripe_subscription_id = EXCLUDED.stripe_subscription_id,
           stripe_customer_id = EXCLUDED.stripe_customer_id,
           sliding_scale_attestation = EXCLUDED.sliding_scale_attestation,
           sliding_scale_amount_cents = EXCLUDED.sliding_scale_amount_cents,
           handoff_build_id = COALESCE(keepers.handoff_build_id, EXCLUDED.handoff_build_id),
           handoff_at = COALESCE(keepers.handoff_at, EXCLUDED.handoff_at),
           updated_at = NOW()
       WHERE keepers.stripe_subscription_id IS NULL
          OR keepers.stripe_subscription_id = EXCLUDED.stripe_subscription_id
          OR ($11::boolean AND keepers.subscription_status <> ALL($12::text[]))
     RETURNING customer_id`,
    [
      claim.customerId,
      claim.companionName,
      claim.companionAssigned,
      claim.status,
      claim.stripeSubscriptionId,
      claim.stripeCustomerId,
      claim.slidingScaleAttestation,
      claim.monthlyAmountCents,
      claim.handoffBuildId,
      claim.handoffAt,
      claim.replaceEnded,
      PUBLISHING_SUBSCRIPTION_STATUSES,
    ]
  );
  return res.rows.length > 0;
}

/**
 * The keeper a released trial's cookie stands for, and the build it paid for:
 * the trial was promoted into the account of the build stamped on it, and
 * that account has a keeper row. Every reader of "whose conversation does a
 * released trial's cookie speak in" uses this one statement (the cookie
 * middleware, and the runtime when a turn outlives the release), so that
 * none of them can take a different view of it. $1 is the trial id.
 *
 * `promoted_customer_id` is what makes the answer a fact and not an
 * inference: a trial that was released without being promoted into this
 * account (or released before the column existed) matches nothing, however its
 * build and the account's keeper row happen to line up.
 */
export const PROMOTED_KEEPER_OF_TRIAL_SQL = `
  SELECT b.id AS build_id, k.customer_id
  FROM trial_keepers t
  JOIN builds b ON b.id = t.handoff_build_id
  JOIN keepers k ON k.customer_id = t.promoted_customer_id
                AND k.customer_id = b.customer_id
  WHERE t.trial_id = $1 AND t.released_at IS NOT NULL`;

/**
 * The trial whose conversation produced this build and has not been promoted
 * yet, or null. Dispatch stamps one trial per build (`handoff_build_id`), and
 * promotion releases it.
 *
 * Two callers need "the conversation that goes with this website" without a
 * trial cookie to tell them: the make-public route, for an owner who logged
 * in before paying, and the payment webhook, for a Checkout session whose
 * metadata names no trial. Both then promote that trial, so the conversation
 * moves into the paying account whichever browser paid.
 */
export async function unreleasedTrialForBuild(
  db: Pick<PoolClient, 'query'>,
  buildId: string
): Promise<string | null> {
  const r = await db.query<{ trial_id: string }>(
    `SELECT trial_id FROM trial_keepers
     WHERE handoff_build_id = $1 AND released_at IS NULL
     ORDER BY handoff_at DESC NULLS LAST
     LIMIT 1`,
    [buildId]
  );
  return r.rows[0]?.trial_id ?? null;
}

export interface PromoteTrialResult {
  /** customers(id) for the keeper. Same as opts.customerId on first promote. */
  customerId: string;
  /** True iff this call performed the promotion; false if it was a no-op duplicate or was refused. */
  promoted: boolean;
  /** Set when the promotion was refused (nothing was written); see PaymentRefusal. Null otherwise. */
  refusal: PaymentRefusal | null;
  /** Number of companion_turns rows transferred from trial_id to keeper_id. */
  conversationTurnsTransferred: number;
  /** Number of keeper_memory rows transferred from trial_id to keeper_id. */
  memoryRowsTransferred: number;
}

export interface PromoteTrialStepResult extends PromoteTrialResult {
  /** False when no trial_keepers row has this id; nothing was written. */
  trialFound: boolean;
}

/**
 * Promote a trial on a client that is already inside a transaction. The
 * caller owns BEGIN, COMMIT and ROLLBACK. A missing trial is reported
 * (`trialFound: false`), not thrown, so the caller can decide what a payment
 * for a vanished trial should do; database errors propagate.
 */
export async function promoteTrialInTransaction(
  client: PoolClient,
  opts: PromoteTrialStepOpts
): Promise<PromoteTrialStepResult> {
  validateOpts(opts);

  // 1. Lock the trial row to prevent concurrent promotion.
  const trialRes = await client.query<{
    trial_id: string;
    companion_name: string;
    companion_assigned: string;
    released_at: Date | null;
    handoff_build_id: string | null;
    handoff_at: Date | null;
  }>(
    `SELECT trial_id, companion_name, companion_assigned, released_at,
            handoff_build_id, handoff_at
     FROM trial_keepers
     WHERE trial_id = $1
     FOR UPDATE`,
    [opts.trialId]
  );

  if (trialRes.rows.length === 0) {
    return {
      customerId: opts.customerId,
      promoted: false,
      refusal: null,
      trialFound: false,
      conversationTurnsTransferred: 0,
      memoryRowsTransferred: 0,
    };
  }

  const trial = trialRes.rows[0];

  // 2. Idempotency: trial already released means an earlier call already
  //    promoted it. Return the existing keeper.
  if (trial.released_at != null) {
    const existingKeeperRes = await client.query<{ customer_id: string }>(
      `SELECT customer_id FROM keepers WHERE customer_id = $1`,
      [opts.customerId]
    );
    return {
      customerId:
        existingKeeperRes.rows.length > 0
          ? existingKeeperRes.rows[0].customer_id
          : opts.customerId,
      promoted: false,
      refusal: null,
      trialFound: true,
      conversationTurnsTransferred: 0,
      memoryRowsTransferred: 0,
    };
  }

  // 3. The trial must be the one stamped on the build that was paid for.
  const refused = (refusal: PaymentRefusal): PromoteTrialStepResult => ({
    customerId: opts.customerId,
    promoted: false,
    refusal,
    trialFound: true,
    conversationTurnsTransferred: 0,
    memoryRowsTransferred: 0,
  });
  if (opts.buildId && (trial.handoff_build_id ?? '').toLowerCase() !== opts.buildId.toLowerCase()) {
    return refused('trial_not_for_build');
  }

  // 4. Create the keeper row and point it at the subscription, and the
  //    trial's build is carried over to it. A trial replaces nothing: the
  //    claim fails, and nothing at all is written, when the account's keeper
  //    already has a subscription of its own.
  //    `handoff_build_id` is what the keeper's conversation page and
  //    POST /lighthouse/build read to know a website already exists; without
  //    it a keeper who just paid would be offered "Build my website" again.
  //    The column name `sliding_scale_amount_cents` is historic; in practice
  //    it stores the keeper's chosen monthly amount whether or not they
  //    attested to a sliding scale (default-$39 payers don't attest, but
  //    we still want their amount on the keeper row so the lamp live-state
  //    can render "$X a month"). The separate `sliding_scale_attestation`
  //    column captures whether the keeper went below the default.
  const claimed = await claimKeeperSubscription(client, {
    customerId: opts.customerId,
    companionName: trial.companion_name,
    companionAssigned: trial.companion_assigned,
    stripeSubscriptionId: opts.stripeSubscriptionId,
    stripeCustomerId: opts.stripeCustomerId,
    status: opts.initialStatus ?? 'active',
    slidingScaleAttestation: opts.slidingScaleAttestation,
    monthlyAmountCents: opts.monthlyAmountCents,
    handoffBuildId: trial.handoff_build_id ?? null,
    handoffAt: trial.handoff_at ?? null,
    replaceEnded: false,
  });
  if (!claimed) return refused('account_has_subscription');

  // 5. Record subscription audit. stripe_subscription_id is UNIQUE; a row
  //    that already exists for it is left as it is.
  const subAuditRes = await client.query(
    `SELECT id FROM harbor_subscriptions WHERE stripe_subscription_id = $1`,
    [opts.stripeSubscriptionId]
  );
  if (subAuditRes.rows.length === 0) {
    await client.query(
      `INSERT INTO harbor_subscriptions
         (keeper_customer_id, stripe_subscription_id, stripe_customer_id,
          status, current_period_start, current_period_end,
          monthly_amount_cents)
       VALUES ($1, $2, $3, $4, $5, $6, $7)`,
      [
        opts.customerId,
        opts.stripeSubscriptionId,
        opts.stripeCustomerId,
        opts.initialStatus ?? 'active',
        opts.currentPeriodStart ?? null,
        opts.currentPeriodEnd ?? null,
        opts.monthlyAmountCents,
      ]
    );
  }

  // 6. Transfer conversation turns. The CHECK on companion_turns
  //    enforces XOR(keeper_id, trial_id) so this single UPDATE swaps
  //    both columns atomically per row.
  //
  //    Turns are moved BEFORE notes (step 7), and that order is relied on:
  //    background memory capture inserts a note under whatever identity its
  //    source turn has, holding a share lock on that turn row
  //    (companion/memory_capture.ts, INSERT_NOTE_FOR_TURN_SQL). A capture that
  //    is in flight either waits for this update and then files its note
  //    under the keeper, or commits first, in which case step 7 runs after it
  //    and moves the note. Swapping the two steps would let a note slip in
  //    under the released trial.
  const turnsRes = await client.query(
    `UPDATE companion_turns
     SET keeper_id = $1, trial_id = NULL
     WHERE trial_id = $2
     RETURNING turn_id`,
    [opts.customerId, opts.trialId]
  );

  // 7. Transfer memory rows (same XOR contract).
  const memoryRes = await client.query(
    `UPDATE keeper_memory
     SET keeper_id = $1, trial_id = NULL
     WHERE trial_id = $2
     RETURNING memory_id`,
    [opts.customerId, opts.trialId]
  );

  // 8. Release the trial, record the account it went to, and keep its cookie
  //    recognizable as the paying browser for PAID_TRIAL_ACCESS_DAYS (see the
  //    header comment).
  await client.query(
    `UPDATE trial_keepers
     SET released_at = NOW(),
         promoted_customer_id = $2,
         expires_at = GREATEST(expires_at, NOW() + INTERVAL '${PAID_TRIAL_ACCESS_DAYS} days')
     WHERE trial_id = $1`,
    [opts.trialId, opts.customerId]
  );

  return {
    customerId: opts.customerId,
    promoted: true,
    refusal: null,
    trialFound: true,
    conversationTurnsTransferred: turnsRes.rowCount ?? 0,
    memoryRowsTransferred: memoryRes.rowCount ?? 0,
  };
}

/**
 * Atomically promote a trial keeper to a permanent keeper paired with
 * the given customers(id), in a transaction of its own. Returns
 * `promoted: false` if the trial has already been released.
 *
 * Throws on a missing trial, FK violations, or any DB error mid-transaction;
 * the transaction is rolled back first.
 */
export async function promoteTrialToKeeper(
  opts: PromoteTrialOpts
): Promise<PromoteTrialResult> {
  validateOpts(opts);

  const client = await opts.pool.connect();
  try {
    await client.query('BEGIN');
    const step = await promoteTrialInTransaction(client, opts);
    if (!step.trialFound) {
      // The catch block handles ROLLBACK + client.release; just throw.
      throw new Error(`promoteTrialToKeeper: no trial_keeper for ${opts.trialId}`);
    }
    await client.query('COMMIT');
    return {
      customerId: step.customerId,
      promoted: step.promoted,
      refusal: step.refusal,
      conversationTurnsTransferred: step.conversationTurnsTransferred,
      memoryRowsTransferred: step.memoryRowsTransferred,
    };
  } catch (err) {
    // A failed ROLLBACK (dead connection) must not replace the real error.
    await client.query('ROLLBACK').catch((rollbackErr: unknown) => {
      console.error(
        '[keeper-promotion] ROLLBACK failed:',
        (rollbackErr as Error).message
      );
    });
    throw err;
  } finally {
    client.release();
  }
}

function validateOpts(opts: PromoteTrialStepOpts): void {
  if (!opts.trialId) {
    throw new Error('promoteTrialToKeeper: trialId is required');
  }
  if (!opts.customerId) {
    throw new Error('promoteTrialToKeeper: customerId is required');
  }
  if (!opts.stripeSubscriptionId) {
    throw new Error('promoteTrialToKeeper: stripeSubscriptionId is required');
  }
  if (!opts.stripeCustomerId) {
    throw new Error('promoteTrialToKeeper: stripeCustomerId is required');
  }
  if (!Number.isFinite(opts.monthlyAmountCents) || opts.monthlyAmountCents < 0) {
    throw new Error(
      `promoteTrialToKeeper: monthlyAmountCents must be a non-negative number; got ${opts.monthlyAmountCents}`
    );
  }
  // Stripe customer + subscription ids look like cus_... and sub_...
  // Light shape check; full validation belongs to the webhook layer.
  if (!opts.stripeCustomerId.startsWith('cus_')) {
    throw new Error(
      `promoteTrialToKeeper: stripeCustomerId should start with 'cus_'; got ${opts.stripeCustomerId.slice(0, 8)}`
    );
  }
  if (!opts.stripeSubscriptionId.startsWith('sub_')) {
    throw new Error(
      `promoteTrialToKeeper: stripeSubscriptionId should start with 'sub_'; got ${opts.stripeSubscriptionId.slice(0, 8)}`
    );
  }
}
