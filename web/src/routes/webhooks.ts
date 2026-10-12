import { Router, Request, Response } from 'express';
import crypto from 'crypto';
import type { PoolClient } from 'pg';
import pool from '../db';
import { constructWebhookEvent } from '../services/stripe';
import {
  sendBuildFailedEmail,
  sendPreviewReadyEmail,
  sendHarborMadePublicEmail,
  sendPaymentNotAppliedEmail,
} from '../services/email';
import {
  promoteTrialInTransaction,
  claimKeeperSubscription,
  unreleasedTrialForBuild,
  keeperSubscriptionStatus,
  PUBLISHING_SUBSCRIPTION_STATUS_LIST_SQL,
  type PaymentRefusal,
} from '../services/keeper_promotion';
import { insertBlessingTurn } from '../companion/blessings';
import { timingSafeEqualStrings } from '../middleware/auth';
import { forwardAsyncErrors } from '../utils/async-routes';
import { hashUnusablePassword } from '../utils/password-hash';
import { armingSwitch } from '../utils/env-switch';

const router = forwardAsyncErrors(Router());

let lastPruneTime = 0;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Work that must happen only after the event's transaction has committed (emails). */
type AfterCommit = () => void;

/**
 * An event that cannot be applied yet because what it refers to has not been
 * recorded (a subscription event that overtook its checkout event). The route
 * rolls the transaction back, claim included, and answers 500 so that Stripe
 * redelivers the event later.
 */
export class StripeEventNotReadyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'StripeEventNotReadyError';
  }
}

/**
 * How long an event for a subscription this database has never heard of is
 * refused (so Stripe retries it) before it is acknowledged and dropped. Its
 * checkout event is created at the same moment, so in the ordinary case the
 * next retry, minutes later, finds the row. Stripe keeps retrying a failed
 * checkout for about three days, and the checkout handler then records the
 * subscription as active. The bound is 72 hours so that a subscription event is
 * still being retried when a checkout succeeds on Stripe's last retry, and is
 * applied rather than dropped. The cost is that a Stripe account which delivers
 * other deployments' events to this endpoint (a shared test-mode account, for
 * example) gets each of them refused for the whole three days.
 */
const UNKNOWN_SUBSCRIPTION_RETRY_WINDOW_SECONDS = 72 * 60 * 60;

/** How long harbor_subscription_tombstones rows are kept (see the prune below). */
const SUBSCRIPTION_TOMBSTONE_RETENTION_DAYS = 90;

/**
 * POST /webhooks/stripe
 *
 * One database transaction per event: the claim row in processed_webhooks
 * (the idempotency record) and every change the event causes commit together
 * or not at all.
 *
 *   - A handler that throws, a failed COMMIT, or a process that dies mid-way
 *     leaves no claim behind, so Stripe's retry is processed from scratch.
 *     There is no "claimed but never applied" state to get stuck in.
 *   - A second delivery of the same event id that arrives while the first is
 *     still in flight blocks on the claim's primary key until the first
 *     transaction ends, then either sees the committed claim (answered as a
 *     duplicate) or, if the first rolled back, takes the claim itself.
 *   - Nothing outside the database happens inside the transaction. Emails are
 *     returned by the handlers and sent after COMMIT.
 */
router.post('/stripe', async (req: Request, res: Response) => {
  const signature = req.headers['stripe-signature'];

  if (!signature) {
    res.status(400).json({ error: 'Missing Stripe signature.' });
    return;
  }

  const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET;
  if (!webhookSecret) {
    console.error('STRIPE_WEBHOOK_SECRET is not configured');
    res.status(500).json({ error: 'Webhook not configured.' });
    return;
  }

  let event;
  try {
    event = constructWebhookEvent(
      req.body as Buffer,
      signature as string,
      webhookSecret
    );
  } catch (err) {
    console.error('Stripe webhook signature verification failed:', (err as Error).message);
    res.status(400).json({ error: 'Invalid signature.' });
    return;
  }

  const eventId = (event as { id?: unknown }).id;
  if (typeof eventId !== 'string' || eventId === '') {
    // Every event Stripe sends has an id. Without one there is nothing to
    // claim, and an unclaimed event could be applied twice.
    res.status(400).json({ error: 'Event has no id.' });
    return;
  }

  let afterCommit: AfterCommit[] = [];
  let connectionUnusable = false;
  let client: PoolClient;
  try {
    client = await pool.connect();
  } catch (err) {
    console.error('Webhook processing error: no database connection:', (err as Error).message);
    res.status(500).json({ error: 'Webhook processing failed.' });
    return;
  }
  try {
    await client.query('BEGIN');
    const claimed = await client.query(
      `INSERT INTO processed_webhooks (event_id, event_type)
       VALUES ($1, $2) ON CONFLICT (event_id) DO NOTHING RETURNING event_id`,
      [eventId, event.type]
    );
    if (claimed.rows.length === 0) {
      await client.query('ROLLBACK');
      console.log(`Webhook: duplicate event ${eventId}, returning 200`);
      res.json({ received: true, duplicate: true });
      return;
    }
    afterCommit = await applyStripeEvent(client, {
      type: event.type,
      created: (event as { created?: unknown }).created,
      object: event.data.object as unknown as Record<string, unknown>,
    });
    await client.query('COMMIT');
  } catch (err) {
    // The 500 tells Stripe to redeliver. The claim was part of the
    // transaction, so rolling back gives it back: the retry is not answered
    // "duplicate".
    try {
      await client.query('ROLLBACK');
    } catch (rollbackErr) {
      // The connection is in an unknown state; it must not go back to the pool.
      connectionUnusable = true;
      console.error('Webhook rollback failed:', (rollbackErr as Error).message);
    }
    if (err instanceof StripeEventNotReadyError) {
      console.warn(`Webhook: event ${eventId} refused for now, Stripe will retry: ${err.message}`);
    } else {
      console.error('Webhook processing error:', (err as Error).message);
    }
    res.status(500).json({ error: 'Webhook processing failed.' });
    return;
  } finally {
    client.release(connectionUnusable ? true : undefined);
  }

  for (const sideEffect of afterCommit) {
    try {
      sideEffect();
    } catch (err) {
      console.error('Webhook post-commit step failed:', (err as Error).message);
    }
  }

  // Periodic prune of old webhook records (at most once per hour). Tombstones
  // of deleted accounts' subscriptions are kept for 90 days: a subscription
  // cancelled at period end sends its last event up to a month later, and
  // Stripe retries a refused event for three days.
  const now = Date.now();
  if (now - lastPruneTime > 3600_000) {
    lastPruneTime = now;
    pool.query(
      "DELETE FROM processed_webhooks WHERE processed_at < NOW() - INTERVAL '7 days'"
    ).catch(err => console.error('Webhook prune failed:', (err as Error).message));
    pool.query(
      `DELETE FROM harbor_subscription_tombstones WHERE removed_at < NOW() - INTERVAL '${SUBSCRIPTION_TOMBSTONE_RETENTION_DAYS} days'`
    ).catch(err => console.error('Subscription tombstone prune failed:', (err as Error).message));
  }

  res.json({ received: true });
});

interface StripeEventInput {
  type: string;
  /** Stripe's `event.created`, epoch seconds. Kept as `unknown` until checked. */
  created: unknown;
  object: Record<string, unknown>;
}

/** Route one claimed event to its handler. Runs inside the route's transaction. */
async function applyStripeEvent(client: PoolClient, event: StripeEventInput): Promise<AfterCommit[]> {
  const createdAt =
    typeof event.created === 'number' && Number.isFinite(event.created) ? Math.floor(event.created) : null;

  switch (event.type) {
    case 'checkout.session.completed': {
      const meta = (event.object.metadata as Record<string, string> | null) || null;
      if (meta && meta.kind === 'harbor_make_public') {
        return handleHarborMakePublic(client, event.object);
      }
      // The only Checkout sessions this application creates are Harbor
      // subscriptions (services/stripe.ts). Anything else on this endpoint
      // belongs to something else on the same Stripe account.
      console.log(`Webhook: checkout session ${String(event.object.id)} is not a Harbor checkout, ignored`);
      return [];
    }

    case 'customer.subscription.updated':
      await handleHarborSubscriptionUpdated(client, event.object, createdAt);
      return [];

    case 'customer.subscription.deleted':
      await handleHarborSubscriptionDeleted(client, event.object, createdAt);
      return [];

    case 'invoice.payment_failed':
      await handleHarborInvoicePaymentFailed(client, event.object, createdAt);
      return [];

    default:
      console.log(`Webhook: unhandled event type ${event.type}`);
      return [];
  }
}

interface MakePublicBuildRow {
  id: string;
  customer_id: string | null;
  app_name: string | null;
  status: string;
  review_status: string | null;
  preview_url: string | null;
  live_url: string | null;
  stripe_session_id: string | null;
}

/**
 * Make-it-public: a Harbor subscription Checkout session was paid.
 *
 * Runs inside the event's transaction, on its client. In order:
 *   1. Lock the build row (FOR UPDATE). Everything below decides and writes
 *      against that locked row, so the "is it publishable" check and the
 *      status change cannot be separated by an admin action or another
 *      delivery.
 *   2. Resolve the customer the payment lands in (the build's customer, or
 *      one created for the Checkout email when the build has none).
 *   3. Promote the trial into that customer (conversation, memory, keeper
 *      row, subscription record), or for a payer who is already a keeper,
 *      point the keeper row at the new subscription. The trial is the one the
 *      Checkout metadata names, or, when it names none, the unreleased trial
 *      stamped with this build: the conversation that produced a website
 *      belongs with it whichever browser paid. The keeper row is written by
 *      one statement that also decides whether it may be (see
 *      `claimKeeperSubscription`): a trial is promoted only into an account
 *      whose keeper has no subscription of its own, and a payment with no
 *      trial replaces only a subscription that no longer publishes. This is
 *      the point of write for the rule the make-public route states when it
 *      opens Checkout (a trial cannot pay into an account that already has a
 *      subscription); a session opened before the account was subscribed
 *      completes here, after the route's check, and meets the rule again.
 *   4. Take the build live: `preview` + `review_status = 'approved'` becomes
 *      `live`. The predicate is in the UPDATE itself; this is the only place
 *      besides POST /builds/:buildId/approve that writes `live`.
 *   5. Record the subscription and Haven's blessing turn.
 * The "your website is public" email is returned to be sent after COMMIT.
 *
 * A paid session is never applied twice (the build remembers the session id
 * that paid for it) and never half applied (one transaction). The cases where
 * money arrived but the site cannot go live are logged as errors and, where a
 * customer is known, leave a harbor_subscriptions row so the charge is
 * visible and later subscription events have something to update, and the
 * operator is sent an alert (`reportUnappliedPayment`), so that a paid session
 * is never dropped without a person being told:
 *   - the build is no longer `preview` + approved (a reviewer asked for
 *     changes, or the build was requeued, while the payer was at Checkout);
 *   - the build is already live and was paid for by a different session;
 *   - the account already has a keeper with a subscription this payment may
 *     not replace (a second trial's session, or a second payment by the
 *     keeper), or the trial the session names is not the one stamped on its
 *     build. The build stays in preview, the trial is not released and the
 *     keeper row is not touched. (When the cause is the keeper's own
 *     subscription, the account's owner can still publish the build under
 *     it.)
 * None is repaired or refunded automatically: refunding is a person's decision
 * made in Stripe, and this code never calls Stripe.
 */
async function handleHarborMakePublic(
  client: PoolClient,
  session: Record<string, unknown>
): Promise<AfterCommit[]> {
  const meta = (session.metadata as Record<string, string> | null) || {};
  const buildId = meta.build_id;
  let trialId: string | null = meta.trial_id || null;
  const monthlyAmountCents = parseInt(meta.monthly_amount_cents || '0', 10);
  const slidingScaleAttestation = meta.sliding_scale_attestation === 'true';
  const sessionId = typeof session.id === 'string' ? session.id : '';

  if (!buildId || !UUID_RE.test(buildId)) {
    console.error('[webhook/harbor] harbor_make_public missing or malformed build_id metadata');
    return [];
  }
  if (!sessionId) {
    console.error('[webhook/harbor] harbor_make_public session has no id');
    return [];
  }
  if (!Number.isInteger(monthlyAmountCents) || monthlyAmountCents <= 0) {
    console.error(`[webhook/harbor] harbor_make_public invalid monthly_amount_cents: ${meta.monthly_amount_cents}`);
    return [];
  }

  // checkout.session.completed also fires for sessions that completed without
  // the money having arrived (delayed payment methods report "unpaid"). The
  // Checkout session here is card-only, so that should not occur, but the
  // site must not go live on the strength of an event that says it has not
  // been paid for. A session with no payment_status at all is let through
  // (Stripe always sends one; only hand-built payloads lack it).
  const paymentStatus = session.payment_status;
  if (typeof paymentStatus === 'string' && paymentStatus !== 'paid') {
    console.error(
      `[webhook/harbor] harbor_make_public session ${sessionId} has payment_status=${paymentStatus}; build ${buildId} left as it is`
    );
    return [];
  }

  const stripeCustomerId = typeof session.customer === 'string' ? session.customer : '';
  const stripeSubscriptionId = typeof session.subscription === 'string' ? session.subscription : '';
  if (!stripeCustomerId.startsWith('cus_') || !stripeSubscriptionId.startsWith('sub_')) {
    console.error('[webhook/harbor] harbor_make_public missing customer or subscription id on session');
    return [];
  }
  const sessionEmail = (
    (session.customer_email as string) ||
    ((session.customer_details as Record<string, unknown> | undefined)?.email as string) ||
    ''
  ).trim().toLowerCase();

  // Step 1: lock the build.
  const buildRes = await client.query<MakePublicBuildRow>(
    `SELECT id, customer_id, app_name, status, review_status, preview_url, live_url,
            stripe_session_id
     FROM builds
     WHERE id = $1
     FOR UPDATE`,
    [buildId]
  );
  if (buildRes.rows.length === 0) {
    return reportUnappliedPayment({
      reason: 'the build it names does not exist, so nothing could be recorded against an account',
      sessionId,
      stripeSubscriptionId,
      buildId,
      customerId: null,
      trialId,
      monthlyAmountCents,
    });
  }
  const build = buildRes.rows[0];

  if (build.stripe_session_id && build.stripe_session_id === sessionId) {
    console.log(`[webhook/harbor] build ${buildId} was already taken live by session ${sessionId}`);
    return [];
  }

  const publishable = build.status === 'preview' && build.review_status === 'approved';
  // Already live without ever having been paid for (an admin approved it
  // while the payer was at Checkout): finish the payer's side, nothing to flip.
  const liveUnpaid = build.status === 'live' && !build.stripe_session_id;

  if (!publishable && !liveUnpaid) {
    console.error(
      `[webhook/harbor] PAID session ${sessionId} (subscription ${stripeSubscriptionId}) cannot take build ${buildId} live: ` +
        `status=${build.status} review=${build.review_status} paid_by=${build.stripe_session_id || 'none'}. ` +
        'The build was left as it is; this payment needs a person to look at it.'
    );
    if (build.customer_id) {
      await recordSubscription(client, {
        customerId: build.customer_id,
        stripeSubscriptionId,
        stripeCustomerId,
        monthlyAmountCents,
      });
    }
    return [
      alertOperator({
        reason: `the build is ${build.status} with review ${build.review_status || 'none'} and was paid for by ${build.stripe_session_id ? 'another session' : 'no session'}, so it cannot be taken live`,
        sessionId,
        stripeSubscriptionId,
        buildId,
        customerId: build.customer_id,
        trialId,
        monthlyAmountCents,
      }),
    ];
  }

  // Step 2: the customer the payment lands in.
  let customerId: string;
  let recipientEmail: string;
  if (build.customer_id) {
    customerId = build.customer_id;
    // The mail goes to the account's address. Whenever the account is
    // decided, the make-public route opens Checkout for exactly that address
    // and refuses any other (routes/lighthouse.ts), and no route changes an
    // account's address, so this is the address the payer was shown and
    // Checkout carried. The session's own email is used only if that
    // customers row cannot be found.
    const owner = await client.query<{ email: string }>(
      'SELECT email FROM customers WHERE id = $1',
      [customerId]
    );
    recipientEmail = owner.rows[0]?.email || sessionEmail;
  } else {
    if (!sessionEmail) {
      return reportUnappliedPayment({
        reason: 'the build has no customer and the session has no email, so nothing could be recorded against an account',
        sessionId,
        stripeSubscriptionId,
        buildId,
        customerId: null,
        trialId,
        monthlyAmountCents,
      });
    }
    customerId = await resolveCustomerByEmail(client, sessionEmail);
    recipientEmail = sessionEmail;
  }

  // Step 3: promote the trial, or attach the subscription to the keeper.
  // A session that names no trial (or names something that is not a trial
  // id) may still have one to promote: the make-public route names the trial
  // whenever it finds one, but this handler does not depend on that. The
  // unreleased trial stamped with this build is that conversation; without
  // promoting it the site would go live and the new keeper's conversation
  // page would be empty.
  if (!trialId || !UUID_RE.test(trialId)) {
    trialId = await unreleasedTrialForBuild(client, buildId);
  }
  let promoted = false;
  let refusal: PaymentRefusal | null = null;
  if (trialId && UUID_RE.test(trialId)) {
    const step = await promoteTrialInTransaction(client, {
      trialId,
      buildId,
      customerId,
      email: recipientEmail,
      stripeSubscriptionId,
      stripeCustomerId,
      monthlyAmountCents,
      slidingScaleAttestation,
      initialStatus: 'active',
    });
    promoted = step.promoted;
    refusal = step.refusal;
    if (!step.trialFound) {
      console.warn(`[webhook/harbor] trial ${trialId} named by session ${sessionId} does not exist; continuing without it`);
    }
    // The account's own keeper paying again (the make-public route records
    // `keeper_customer_id` only for a session opened under the account's login
    // or by the browser that paid for it) is not a trial paying into someone
    // else's keeper. When the keeper already has a subscription, the trial is
    // not merged into it, but the payment is still the keeper's: it falls
    // through to the claim below, which takes over a subscription that no
    // longer publishes and refuses one that still does.
    if (refusal === 'account_has_subscription' && meta.keeper_customer_id === customerId) refusal = null;
  }
  if (!promoted && !refusal) {
    // Reached when the payer was already a keeper (no trial in the metadata)
    // and when the named trial is gone or was released earlier. The keeper
    // row must exist before the blessing turn is written
    // (companion_turns.keeper_id references it) and must point at the
    // subscription that was just paid, unless it already has one that still
    // publishes: that keeper does not need a second subscription, and this
    // payment is not made theirs.
    const claimed = await claimKeeperSubscription(client, {
      customerId,
      companionName: 'Haven',
      companionAssigned: 'Haven',
      stripeSubscriptionId,
      stripeCustomerId,
      status: 'active',
      slidingScaleAttestation,
      monthlyAmountCents,
      handoffBuildId: null,
      handoffAt: null,
      replaceEnded: true,
    });
    if (!claimed) refusal = 'account_has_subscription';
  }
  if (refusal) {
    // Money has arrived and may not be applied. Nothing the promotion or the
    // claim would have written was written. The payment is recorded against
    // the account so that the events Stripe sends for it find a row (they
    // are mirrored onto the keeper row only while it points at that
    // subscription, which it does not), and a person is told.
    await recordSubscription(client, { customerId, stripeSubscriptionId, stripeCustomerId, monthlyAmountCents });
    return reportUnappliedPayment({
      reason: REFUSAL_REASONS[refusal],
      sessionId,
      stripeSubscriptionId,
      buildId,
      customerId,
      trialId,
      monthlyAmountCents,
    });
  }

  // Step 4: take the build live (or, for one that already is, record who paid).
  if (publishable) {
    const flipRes = await client.query<{ id: string }>(
      `UPDATE builds
         SET status = 'live',
             live_url = COALESCE(live_url, preview_url),
             customer_id = COALESCE(customer_id, $2),
             stripe_session_id = $3
       WHERE id = $1 AND status = 'preview' AND review_status = 'approved'
       RETURNING id`,
      [buildId, customerId, sessionId]
    );
    if (flipRes.rows.length === 0) {
      // The row is locked and was publishable a moment ago, so this cannot
      // happen. If it ever does, fail the whole event rather than commit a
      // promotion with nothing live.
      throw new Error(`[webhook/harbor] build ${buildId} did not go live although it was locked at preview+approved`);
    }
  } else {
    await client.query(
      `UPDATE builds
         SET customer_id = COALESCE(customer_id, $2),
             stripe_session_id = $3
       WHERE id = $1 AND status = 'live' AND stripe_session_id IS NULL`,
      [buildId, customerId, sessionId]
    );
  }

  // Step 5: subscription record (the trial path wrote it during promotion;
  // this covers the keeper path) and the blessing turn. Seed = trialId when
  // there is one, buildId otherwise, so a given payer always gets the same
  // blessing.
  await recordSubscription(client, { customerId, stripeSubscriptionId, stripeCustomerId, monthlyAmountCents });
  await insertBlessingTurn(client, { keeperId: customerId, seed: trialId || buildId });

  console.log(
    `[webhook/harbor] build ${buildId} made public (customer=${customerId} sub=${stripeSubscriptionId} trial=${trialId || 'none'} promoted=${promoted})`
  );

  const publicUrl = build.live_url || build.preview_url || '';
  const appName = build.app_name || 'Your website';
  if (!recipientEmail) return [];
  return [
    () => {
      // Fire-and-forget: a mail failure is logged and never becomes a webhook
      // error (the event is already committed).
      sendHarborMadePublicEmail({
        to: recipientEmail,
        name: recipientEmail.split('@')[0],
        appName,
        publicUrl,
        buildId,
        monthlyAmountCents,
      }).catch((err) =>
        console.error('[webhook/harbor] sendHarborMadePublicEmail failed (non-blocking):', (err as Error).message)
      );
    },
  ];
}

const REFUSAL_REASONS: Record<PaymentRefusal, string> = {
  account_has_subscription:
    'the account already has a keeper with a subscription that this payment may not replace, so the keeper row, the trial and the build were left as they were',
  trial_not_for_build:
    'the trial the session names is not the one stamped on its build, so nothing was promoted and the build was left as it was',
};

interface UnappliedPayment {
  /** Plain words for the operator: why the money was not applied. */
  reason: string;
  sessionId: string;
  stripeSubscriptionId: string;
  buildId: string;
  customerId: string | null;
  trialId: string | null;
  monthlyAmountCents: number;
}

/**
 * The after-commit step that tells the operator a paid session was not
 * applied. It is fire-and-forget like every mail here: a mail failure is
 * logged and never becomes a webhook error, and the event stays committed.
 */
function alertOperator(payment: UnappliedPayment): AfterCommit {
  return () => {
    sendPaymentNotAppliedEmail(payment).catch((err) =>
      console.error('[webhook/harbor] sendPaymentNotAppliedEmail failed (non-blocking):', (err as Error).message)
    );
  };
}

/** Log a paid session that was not applied, and return the step that alerts the operator. */
function reportUnappliedPayment(payment: UnappliedPayment): AfterCommit[] {
  console.error(
    `[webhook/harbor] PAID session ${payment.sessionId} (subscription ${payment.stripeSubscriptionId}) for build ${payment.buildId} was not applied: ${payment.reason}. ` +
      'This payment needs a person to look at it.'
  );
  return [alertOperator(payment)];
}

/** Insert the harbor_subscriptions row for a subscription unless it already has one. */
async function recordSubscription(
  client: PoolClient,
  sub: { customerId: string; stripeSubscriptionId: string; stripeCustomerId: string; monthlyAmountCents: number }
): Promise<void> {
  await client.query(
    `INSERT INTO harbor_subscriptions
       (keeper_customer_id, stripe_subscription_id, stripe_customer_id,
        status, monthly_amount_cents)
     VALUES ($1, $2, $3, 'active', $4)
     ON CONFLICT (stripe_subscription_id) DO NOTHING`,
    [sub.customerId, sub.stripeSubscriptionId, sub.stripeCustomerId, sub.monthlyAmountCents]
  );
}

/**
 * The customers row for a Checkout email: the existing one, or a new
 * unverified one. `password` and `name` are NOT NULL, so a new row gets a
 * bcrypt hash of 64 random bytes that nobody ever sees (the account is
 * entered through a password-reset or verification link sent to the mailbox)
 * and a name taken from the email's local part.
 */
async function resolveCustomerByEmail(client: PoolClient, email: string): Promise<string> {
  const ephemeralPassword = await hashUnusablePassword();
  const ephemeralName = (email.split('@')[0] || 'Keeper').slice(0, 60);
  const inserted = await client.query<{ id: string }>(
    `INSERT INTO customers (email, password, email_verified, name)
     VALUES ($1, $2, FALSE, $3)
     ON CONFLICT (email) DO NOTHING
     RETURNING id`,
    [email, ephemeralPassword, ephemeralName]
  );
  if (inserted.rows.length > 0) return inserted.rows[0].id;
  const existing = await client.query<{ id: string }>(
    'SELECT id FROM customers WHERE email = $1',
    [email]
  );
  if (existing.rows.length === 0) {
    throw new Error('[webhook/harbor] could not resolve a customer for the checkout email');
  }
  return existing.rows[0].id;
}

// ---------------------------------------------------------------------------
// Subscription lifecycle events
// ---------------------------------------------------------------------------
//
// Three rules hold for every statement below:
//
//  1. Terminal state. `canceled` is final at Stripe (a canceled subscription
//     never becomes active again; a new one has a new id), so no event moves
//     a row out of `canceled`. A late or redelivered `updated` or
//     `invoice.payment_failed` after `deleted` changes nothing.
//  2. Order. Stripe does not deliver events in order, and a retried delivery
//     can arrive after newer ones. Each applied event stores its
//     `event.created` in harbor_subscriptions.last_event_created, and an
//     event older than what the row already reflects is skipped. This needs
//     that column; see `eventOrderingAvailable`.
//  3. One statement. The audit row (harbor_subscriptions) and its mirror on
//     the keeper row change in a single statement, and the keeper row is
//     touched only while it keeps pointing at this subscription, so an event
//     for an old subscription cannot overwrite the state of a newer one.

let orderingColumnState: { present: boolean; checkedAtMs: number } | null = null;
const ORDERING_COLUMN_RECHECK_MS = 60_000;

/**
 * Whether harbor_subscriptions.last_event_created exists in this database.
 * The ordering rule above depends on it. A database that does not have the
 * column yet still gets rules 1 and 3; the gap is logged at each recheck. A
 * positive answer is cached for the life of the process, a negative one for
 * a minute.
 */
async function eventOrderingAvailable(client: PoolClient): Promise<boolean> {
  if (orderingColumnState && orderingColumnState.present) return true;
  if (orderingColumnState && Date.now() - orderingColumnState.checkedAtMs < ORDERING_COLUMN_RECHECK_MS) {
    return false;
  }
  const res = await client.query(
    `SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'harbor_subscriptions'
       AND column_name = 'last_event_created'`
  );
  const present = res.rows.length > 0;
  orderingColumnState = { present, checkedAtMs: Date.now() };
  if (!present) {
    console.warn(
      '[webhook/harbor-sub] harbor_subscriptions.last_event_created is missing: out-of-order subscription events are not detected'
    );
  }
  return present;
}

/** Forget the cached answer of `eventOrderingAvailable`. For tests that change the schema. */
export function resetEventOrderingCache(): void {
  orderingColumnState = null;
}

interface SubscriptionWriteResult {
  applied: number;
  mirrored: number;
}

/**
 * Decide what to do with a subscription event whose UPDATE matched no row:
 * the subscription is unknown here (refuse for now, see
 * UNKNOWN_SUBSCRIPTION_RETRY_WINDOW_SECONDS), or the row exists and one of
 * the rules above skipped the event.
 */
async function explainSkippedSubscriptionEvent(
  client: PoolClient,
  kind: string,
  stripeSubscriptionId: string,
  createdAt: number | null
): Promise<void> {
  const existing = await client.query<{ status: string }>(
    'SELECT status FROM harbor_subscriptions WHERE stripe_subscription_id = $1',
    [stripeSubscriptionId]
  );
  if (existing.rows.length > 0) {
    console.log(
      `[webhook/harbor-sub] ${kind} for ${stripeSubscriptionId} skipped: the row is ${existing.rows[0].status} and already reflects this or a newer event`
    );
    return;
  }
  // The row may have been removed on purpose: deleting an account cascades to
  // its harbor_subscriptions rows and leaves a tombstone for each (POST
  // /account/delete in routes/auth.ts). Stripe still reports the end of such a
  // subscription; there is nothing left to update, and asking for a retry
  // would only repeat this.
  const tombstone = await client.query(
    'SELECT 1 FROM harbor_subscription_tombstones WHERE stripe_subscription_id = $1',
    [stripeSubscriptionId]
  );
  if (tombstone.rows.length > 0) {
    console.log(
      `[webhook/harbor-sub] ${kind} for ${stripeSubscriptionId} acknowledged: its account was deleted and the subscription record went with it`
    );
    return;
  }
  const ageSeconds = createdAt === null ? Infinity : Math.floor(Date.now() / 1000) - createdAt;
  if (ageSeconds < UNKNOWN_SUBSCRIPTION_RETRY_WINDOW_SECONDS) {
    throw new StripeEventNotReadyError(
      `${kind} for subscription ${stripeSubscriptionId}, which has no harbor_subscriptions row yet`
    );
  }
  console.error(
    `[webhook/harbor-sub] ${kind} for unknown subscription ${stripeSubscriptionId} dropped: no checkout event recorded it within ${UNKNOWN_SUBSCRIPTION_RETRY_WINDOW_SECONDS}s`
  );
}

/**
 * Stripe customer.subscription.updated: mirror status, cancel_at_period_end
 * and the period dates into harbor_subscriptions and the keeper row. Only
 * Harbor subscriptions (metadata.kind) are considered.
 */
async function handleHarborSubscriptionUpdated(
  client: PoolClient,
  sub: Record<string, unknown>,
  createdAt: number | null
): Promise<void> {
  const meta = (sub.metadata as Record<string, string> | null) || {};
  if (meta.kind !== 'harbor_make_public') {
    console.log('[webhook/harbor-sub] subscription.updated not a harbor sub, skipping');
    return;
  }
  const stripeSubscriptionId = sub.id as string;
  const status = (sub.status as string) || 'unknown';
  const cancelAtPeriodEnd = Boolean(sub.cancel_at_period_end);
  const currentPeriodStart = typeof sub.current_period_start === 'number'
    ? new Date((sub.current_period_start as number) * 1000) : null;
  const currentPeriodEnd = typeof sub.current_period_end === 'number'
    ? new Date((sub.current_period_end as number) * 1000) : null;
  const keeperStatus = keeperSubscriptionStatus(status);

  const ordered = createdAt !== null && (await eventOrderingAvailable(client));
  const params: unknown[] = [
    status, cancelAtPeriodEnd, currentPeriodStart, currentPeriodEnd, stripeSubscriptionId, keeperStatus,
  ];
  if (ordered) params.push(createdAt);
  const res = await client.query<SubscriptionWriteResult>(
    `WITH applied AS (
       UPDATE harbor_subscriptions
          SET status = $1,
              cancel_at_period_end = $2,
              current_period_start = $3,
              current_period_end = $4,
              ${ordered ? 'last_event_created = $7,' : ''}
              updated_at = NOW()
        WHERE stripe_subscription_id = $5
          AND status <> 'canceled'
          ${ordered ? 'AND (last_event_created IS NULL OR last_event_created <= $7)' : ''}
        RETURNING keeper_customer_id
     ), mirrored AS (
       UPDATE keepers k
          SET subscription_status = $6, updated_at = NOW()
         FROM applied
        WHERE k.customer_id = applied.keeper_customer_id
          AND k.stripe_subscription_id = $5
        RETURNING k.customer_id
     )
     SELECT (SELECT COUNT(*) FROM applied)::int AS applied,
            (SELECT COUNT(*) FROM mirrored)::int AS mirrored`,
    params
  );
  if (!res.rows[0] || res.rows[0].applied === 0) {
    await explainSkippedSubscriptionEvent(client, 'subscription.updated', stripeSubscriptionId, createdAt);
    return;
  }
  console.log(
    `[webhook/harbor-sub] subscription.updated sub=${stripeSubscriptionId} status=${status} cancel_at_period_end=${cancelAtPeriodEnd} keeper_rows=${res.rows[0].mirrored}`
  );
}

/**
 * Stripe customer.subscription.deleted: the subscription has fully ended
 * (period close after cancel_at_period_end, or an immediate cancel). The
 * website stays up: the build's status does not change. The row and its
 * keeper mirror become `canceled`, which is final (rule 1), so this event
 * applies whatever order it arrives in.
 */
async function handleHarborSubscriptionDeleted(
  client: PoolClient,
  sub: Record<string, unknown>,
  createdAt: number | null
): Promise<void> {
  const meta = (sub.metadata as Record<string, string> | null) || {};
  if (meta.kind !== 'harbor_make_public') {
    console.log('[webhook/harbor-sub] subscription.deleted not a harbor sub, skipping');
    return;
  }
  const stripeSubscriptionId = sub.id as string;

  const ordered = createdAt !== null && (await eventOrderingAvailable(client));
  const params: unknown[] = [stripeSubscriptionId];
  if (ordered) params.push(createdAt);
  const res = await client.query<SubscriptionWriteResult>(
    `WITH applied AS (
       UPDATE harbor_subscriptions
          SET status = 'canceled',
              cancel_at_period_end = FALSE,
              ${ordered ? 'last_event_created = GREATEST(COALESCE(last_event_created, 0), $2),' : ''}
              updated_at = NOW()
        WHERE stripe_subscription_id = $1
        RETURNING keeper_customer_id
     ), mirrored AS (
       UPDATE keepers k
          SET subscription_status = 'canceled', updated_at = NOW()
         FROM applied
        WHERE k.customer_id = applied.keeper_customer_id
          AND k.stripe_subscription_id = $1
        RETURNING k.customer_id
     )
     SELECT (SELECT COUNT(*) FROM applied)::int AS applied,
            (SELECT COUNT(*) FROM mirrored)::int AS mirrored`,
    params
  );
  if (!res.rows[0] || res.rows[0].applied === 0) {
    await explainSkippedSubscriptionEvent(client, 'subscription.deleted', stripeSubscriptionId, createdAt);
    return;
  }
  console.log(`[webhook/harbor-sub] subscription.deleted sub=${stripeSubscriptionId} keeper_rows=${res.rows[0].mirrored}`);
}

/**
 * Stripe invoice.payment_failed: Stripe handles smart retries and eventual
 * cancellation; this mirrors the `past_due` signal so the lamp page can show
 * a "the card needs updating" note. The website is not taken down.
 *
 * A failed invoice only ever takes something away. It moves a subscription
 * that is in force (`active`, `trialing`, or already `past_due`) to
 * `past_due`, and leaves every other status alone: an `incomplete` first
 * payment that failed, or a subscription Stripe has already moved to `unpaid`
 * in the same second, must not become `past_due`, which still publishes
 * (PUBLISHING_SUBSCRIPTION_STATUSES). The subscription's real status arrives
 * in `customer.subscription.updated`.
 *
 * An invoice for a subscription that has no row here is not a Harbor invoice
 * as far as this database knows, and is acknowledged without a retry: the
 * `customer.subscription.updated` event that Stripe sends for the same
 * failure carries the status and is the one that waits for the row.
 */
async function handleHarborInvoicePaymentFailed(
  client: PoolClient,
  invoice: Record<string, unknown>,
  createdAt: number | null
): Promise<void> {
  const stripeSubscriptionId = typeof invoice.subscription === 'string' ? invoice.subscription : '';
  if (!stripeSubscriptionId) {
    console.log('[webhook/harbor-sub] invoice.payment_failed without subscription id, skipping');
    return;
  }

  const ordered = createdAt !== null && (await eventOrderingAvailable(client));
  const params: unknown[] = [stripeSubscriptionId];
  if (ordered) params.push(createdAt);
  const res = await client.query<SubscriptionWriteResult>(
    `WITH applied AS (
       UPDATE harbor_subscriptions
          SET status = 'past_due',
              ${ordered ? 'last_event_created = $2,' : ''}
              updated_at = NOW()
        WHERE stripe_subscription_id = $1
          AND status IN (${PUBLISHING_SUBSCRIPTION_STATUS_LIST_SQL})
          ${ordered ? 'AND (last_event_created IS NULL OR last_event_created <= $2)' : ''}
        RETURNING keeper_customer_id
     ), mirrored AS (
       UPDATE keepers k
          SET subscription_status = 'past_due', updated_at = NOW()
         FROM applied
        WHERE k.customer_id = applied.keeper_customer_id
          AND k.stripe_subscription_id = $1
          AND k.subscription_status IN (${PUBLISHING_SUBSCRIPTION_STATUS_LIST_SQL})
        RETURNING k.customer_id
     )
     SELECT (SELECT COUNT(*) FROM applied)::int AS applied,
            (SELECT COUNT(*) FROM mirrored)::int AS mirrored`,
    params
  );
  if (!res.rows[0] || res.rows[0].applied === 0) {
    console.log(
      `[webhook/harbor-sub] invoice.payment_failed for ${stripeSubscriptionId} changed nothing (not a recorded Harbor subscription, not in force, or older than the recorded state)`
    );
    return;
  }
  console.log(`[webhook/harbor-sub] invoice.payment_failed -> past_due sub=${stripeSubscriptionId} keeper_rows=${res.rows[0].mirrored}`);
}

/**
 * Pipeline statuses POST /webhooks/build-status may write, with the statuses a
 * build must currently be in for each write to apply. `live` is deliberately
 * absent: a build becomes live only through POST /builds/:buildId/approve or
 * the Stripe make-public handler (both require a recorded review verdict,
 * except the admin's own approve), never through this hook.
 * Every write below is an UPDATE guarded by these predecessors, so the hook
 * cannot move a build backwards or resurrect a failed, live or abandoned one.
 */
const BUILD_STATUS_PREDECESSORS: Record<string, string[]> = {
  building: ['queued', 'building'],
  testing: ['building', 'testing'],
  deploying: ['building', 'testing', 'deploying'],
  preview: ['building', 'testing', 'deploying'],
  failed: ['queued', 'building', 'testing', 'deploying'],
};

/**
 * The review_status a build gets each time it arrives at preview: `pending`
 * (a person reviews it at /admin/reviews), or `ai_review_queued` when
 * CAS_HARBOR_AUTO_REVIEW is switched on, for an automated review loop that is
 * not part of this repository. It is the web side of
 * `_initial_review_status` in queue/worker_pg.py, which makes the same
 * decision for the build the bundled worker lands at preview. The two are
 * written in different languages and cannot share a function, so they share
 * the rule instead: the switch is an arming switch, read by the parser that
 * tests/unit/env-switch.test.ts compares with the worker's word for word, and
 * money-path.realdb.test.ts lands one build through each writer and compares
 * the rows.
 */
export function initialReviewStatus(): 'pending' | 'ai_review_queued' {
  return armingSwitch(process.env.CAS_HARBOR_AUTO_REVIEW) ? 'ai_review_queued' : 'pending';
}

/**
 * The address of the deployment a status report carries: `preview_url`, or
 * `live_url`, the name this field was first given and an external worker may
 * still send. The address is a preview's (it is written to
 * builds.preview_url; a build's live_url is set only when it is published),
 * which is why the name `live_url` was misleading. When both are sent,
 * `preview_url` wins.
 */
function reportedPreviewUrl(body: Record<string, unknown>): { url: string; field: 'preview_url' | 'live_url' } {
  if (typeof body.preview_url === 'string' && body.preview_url !== '') return { url: body.preview_url, field: 'preview_url' };
  if (typeof body.live_url === 'string' && body.live_url !== '') return { url: body.live_url, field: 'live_url' };
  return { url: '', field: 'preview_url' };
}

// Optional authenticated status hook (Bearer WORKER_WEBHOOK_SECRET). The
// bundled worker does not call it: it writes the builds table directly. It is
// kept for an external worker that reports progress over HTTP.
router.post('/build-status', async (req: Request, res: Response) => {
  const workerSecret = process.env.WORKER_WEBHOOK_SECRET;
  const authHeader = req.headers['authorization'];

  if (!workerSecret) {
    console.error('WORKER_WEBHOOK_SECRET not configured; rejecting build-status webhook');
    res.status(500).json({ error: 'Webhook not configured.' });
    return;
  }

  if (!timingSafeEqualStrings(authHeader || '', `Bearer ${workerSecret}`)) {
    res.status(401).json({ error: 'Unauthorized.' });
    return;
  }

  const body = (req.body || {}) as Record<string, unknown>;
  const build_id = typeof body.build_id === 'string' ? body.build_id : '';
  const status = typeof body.status === 'string' ? body.status : '';
  const { url: preview_url, field: previewUrlField } = reportedPreviewUrl(body);

  if (!build_id || !status) {
    res.status(400).json({ error: 'build_id and status are required.' });
    return;
  }

  // The URL is written into emails and into href attributes on the dashboard.
  if (preview_url && !/^https?:\/\//i.test(preview_url)) {
    res.status(400).json({ error: `${previewUrlField} must be an http(s) URL.` });
    return;
  }

  if (!Object.prototype.hasOwnProperty.call(BUILD_STATUS_PREDECESSORS, status)) {
    res.status(400).json({ error: 'Invalid status.' });
    return;
  }

  try {
    const buildResult = await pool.query(
      `SELECT b.id, b.customer_id, b.app_name, b.status as current_status,
              c.email, c.name
       FROM builds b
       JOIN customers c ON b.customer_id = c.id
       WHERE b.id = $1`,
      [build_id]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const build = buildResult.rows[0] as {
      id: string;
      app_name: string | null;
      current_status: string;
      email: string;
      name: string;
    };

    const predecessors = BUILD_STATUS_PREDECESSORS[status];
    const withPreviewUrl = status === 'preview' && preview_url !== '';
    // Arriving at preview means a new deployment exists, so the build goes
    // (back) into review (initialReviewStatus), with whatever an earlier
    // review recorded cleared. This is what the bundled worker does when it
    // lands a build at preview (_set_preview_status in queue/worker_pg.py).
    const updated = status === 'preview'
      ? await pool.query(
          `UPDATE builds
              SET status = 'preview',
                  preview_url = COALESCE($1, preview_url),
                  review_status = $4,
                  reviewed_at = NULL,
                  reviewed_by = NULL,
                  review_notes = NULL,
                  review_report = NULL,
                  ai_review_iterations = 0,
                  ai_reviewed_at = NULL
            WHERE id = $2 AND status = ANY($3::text[])`,
          [withPreviewUrl ? preview_url : null, build_id, predecessors, initialReviewStatus()]
        )
      : await pool.query(
          `UPDATE builds SET status = $1
           WHERE id = $2 AND status = ANY($3::text[])`,
          [status, build_id, predecessors]
        );

    if (!updated.rowCount) {
      // The row exists (checked above) but is not in a state this transition
      // may start from: a repeat, a late report, or a finished build.
      res.status(409).json({ error: `Build is ${build.current_status}; it cannot move to ${status}.` });
      return;
    }

    console.log(`Build ${build_id} status updated to ${status}`);

    if (withPreviewUrl) {
      sendPreviewReadyEmail(build.email, build.name, build.app_name || build_id, preview_url, build_id, build.email).catch(err =>
        console.error('[webhook] Preview ready email failed:', (err as Error).message)
      );
    } else if (status === 'failed') {
      sendBuildFailedEmail(build.email, build.name, build.app_name || build_id).catch(err =>
        console.error('[webhook] Build failed email failed:', (err as Error).message)
      );
    }

    res.json({ ok: true });
  } catch (err) {
    console.error('Build status webhook error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to update build status.' });
  }
});

const SVIX_TOLERANCE_SECONDS = 5 * 60;

/**
 * Verify a Svix-signed webhook (the scheme Resend uses). The signature is
 * base64(HMAC-SHA256(key, `${id}.${timestamp}.${payload}`)) where key is the
 * base64 part of the "whsec_..." secret. The header may carry several
 * space-separated "v1,<sig>" entries (during secret rotation); any match wins.
 * The timestamp must be within SVIX_TOLERANCE_SECONDS of now, which bounds how
 * long a captured delivery can be replayed. Exported for tests.
 */
export function verifySvixSignature(input: {
  secret: string;
  id: unknown;
  timestamp: unknown;
  signatureHeader: unknown;
  payload: string;
  nowSeconds?: number;
}): 'ok' | 'missing' | 'stale' | 'invalid' {
  const { secret, id, timestamp, signatureHeader, payload } = input;
  if (typeof id !== 'string' || typeof timestamp !== 'string' || typeof signatureHeader !== 'string' ||
      !id || !timestamp || !signatureHeader) {
    return 'missing';
  }
  const sentAt = Number(timestamp);
  const now = input.nowSeconds ?? Math.floor(Date.now() / 1000);
  if (!Number.isFinite(sentAt) || Math.abs(now - sentAt) > SVIX_TOLERANCE_SECONDS) {
    return 'stale';
  }
  const keyMaterial = secret.startsWith('whsec_') ? secret.slice('whsec_'.length) : secret;
  const expected = crypto
    .createHmac('sha256', Buffer.from(keyMaterial, 'base64'))
    .update(`${id}.${timestamp}.${payload}`)
    .digest('base64');
  const candidates = signatureHeader.split(' ').map(entry => entry.replace(/^v1,/, ''));
  return candidates.some(candidate => timingSafeEqualStrings(candidate, expected)) ? 'ok' : 'invalid';
}

// Resend email delivery webhook: tracks actual delivery, bounce, and complaint events.
// Configure in Resend dashboard: POST https://craftandship.io/webhooks/resend
// Signing secret stored in RESEND_WEBHOOK_SECRET env var.
router.post('/resend', async (req: Request, res: Response) => {
  const webhookSecret = process.env.RESEND_WEBHOOK_SECRET;

  // Fail closed: without the signing secret there is no way to tell Resend
  // from anyone else, and this endpoint rewrites delivery status.
  if (!webhookSecret) {
    console.error('RESEND_WEBHOOK_SECRET not configured; rejecting resend webhook');
    res.status(500).json({ error: 'Webhook not configured.' });
    return;
  }

  const rawBody = (req as Request & { rawBody?: Buffer }).rawBody;
  const verified = verifySvixSignature({
    secret: webhookSecret,
    id: req.headers['svix-id'],
    timestamp: req.headers['svix-timestamp'],
    signatureHeader: req.headers['svix-signature'],
    // app.ts keeps the exact request bytes for this route. The JSON.stringify
    // fallback only matters when the router is mounted without that hook.
    payload: rawBody ? rawBody.toString('utf8') : JSON.stringify(req.body),
  });
  if (verified === 'missing') {
    res.status(401).json({ error: 'Missing webhook signature headers.' });
    return;
  }
  if (verified !== 'ok') {
    res.status(401).json({ error: 'Invalid webhook signature.' });
    return;
  }

  const { type, data } = req.body as {
    type?: string;
    data?: { email_id?: string; to?: string[] };
  };

  if (!type || !data?.email_id) {
    res.status(400).json({ error: 'Invalid webhook payload.' });
    return;
  }

  // Map Resend event types to our delivery_status values
  const STATUS_MAP: Record<string, string> = {
    'email.delivered': 'delivered',
    'email.bounced': 'bounced',
    'email.complained': 'complained',
    'email.delivery_delayed': 'delayed',
  };

  const deliveryStatus = STATUS_MAP[type];
  if (!deliveryStatus) {
    // Ignore event types we do not track (e.g. email.sent, email.opened, email.clicked)
    res.json({ ok: true, ignored: true });
    return;
  }

  try {
    const result = await pool.query(
      `UPDATE email_log
       SET delivery_status = $1, delivery_updated_at = NOW()
       WHERE resend_id = $2`,
      [deliveryStatus, data.email_id]
    );

    if (result.rowCount === 0) {
      console.warn(`[resend-webhook] No email_log row for resend_id=${data.email_id}`);
    } else {
      console.info(`[resend-webhook] ${data.email_id} -> ${deliveryStatus}`);
    }

    res.json({ ok: true });
  } catch (err) {
    console.error('[resend-webhook] Error:', (err as Error).message);
    res.status(500).json({ error: 'Internal error.' });
  }
});

export default router;
