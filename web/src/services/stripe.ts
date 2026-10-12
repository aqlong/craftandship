import Stripe from 'stripe';

// Every call this module makes to Stripe is made while a visitor's request
// waits (the make-public form, the cancel button, account deletion), so each
// has a deadline. The SDK's defaults are 80 seconds an attempt and one retry,
// which holds a request open for close to three minutes when Stripe stalls;
// here it is 20 seconds an attempt and one retry. At the deadline the call
// rejects and the route answers as it does for any other Stripe failure.
const STRIPE_TIMEOUT_MS = 20_000;
const STRIPE_MAX_NETWORK_RETRIES = 1;

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY || '', {
  apiVersion: '2023-10-16',
  timeout: STRIPE_TIMEOUT_MS,
  maxNetworkRetries: STRIPE_MAX_NETWORK_RETRIES,
});

// This module creates no Stripe customer of its own. A Checkout session in
// subscription mode creates one when the payer completes it, so an abandoned
// or repeated make-public form leaves nothing behind at Stripe except the
// unpaid session, which expires on its own.

/**
 * Pricing band for the make-it-public Stripe subscription.
 * Standard is $39/month; sliding scale floor is $19; the keeper picks
 * any whole-dollar amount in [1900, 3900] cents. Server validates
 * before calling this; client validates too but we double-check here.
 */
export const HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS = 1900;
export const HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS = 3900;
export const HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS = 3900;

/**
 * What the payer reads on Stripe's Checkout page and on their receipts: the
 * product's name and description. Customer copy like any other
 * (docs/BRAND_VOICE.md): no internal names for the product or the customer,
 * and the review is described as the rest of the site describes it, with no
 * reviewer named. stripe.service.test.ts reads both from the request sent to
 * Stripe and holds them to that, and to what the terms page promises.
 */
const HARBOR_SUBSCRIPTION_PRODUCT_NAME = 'Craft & Ship website subscription';
const HARBOR_SUBSCRIPTION_PRODUCT_DESCRIPTION =
  'Your website stays up at its address. The subscription covers hosting, a recorded review approval for every website before it goes live, unlimited revisions and rebuilds, and Haven, your AI companion.';

export interface HarborSubscriptionCheckoutOpts {
  /** builds.id (UUID); flows through metadata so the webhook can flip the build to live. */
  buildId: string;
  /** trial_keepers.trial_id (UUID) when promoting from a trial; null when paying as already-promoted keeper. */
  trialId: string | null;
  /** customers.id (UUID) when one already exists; null pre-promotion. */
  keeperCustomerId: string | null;
  /** The Stripe customer (cus_...) already on file for this keeper, or null.
   * With null, Checkout creates the customer from `customerEmail` when the
   * session is paid. Only an id this application stored for the same account
   * may be passed: Checkout offers a returning customer's saved card, so a
   * Stripe customer must never be looked up by an email someone typed. */
  stripeCustomerId: string | null;
  /** Email for the new Stripe customer; shown, not editable, on the Checkout page. Used when `stripeCustomerId` is null. */
  customerEmail: string;
  /** Whole-dollar cents in [1900, 3900]. */
  monthlyAmountCents: number;
  /** True iff keeper checked the sliding-scale attestation; required when amount < 3900. */
  slidingScaleAttestation: boolean;
  /** Origin (https://craftandship.io or http://localhost:4000) for success/cancel URLs. */
  baseUrl: string;
}

/**
 * Build a Stripe Checkout session in subscription mode for the
 * make-it-public payment moment. Returns the hosted-checkout URL.
 *
 * The webhook handler dispatches on `metadata.kind === 'harbor_make_public'`
 * to know which path completed. The same metadata is set on the session and
 * on `subscription_data.metadata`, so the subscription's own events
 * (updated, deleted) can be recognized as Harbor's.
 *
 * Throws if Stripe rejects the request or returns a null URL.
 */
export async function createHarborSubscriptionCheckoutSession(
  opts: HarborSubscriptionCheckoutOpts
): Promise<string> {
  const {
    buildId,
    trialId,
    keeperCustomerId,
    stripeCustomerId,
    customerEmail,
    monthlyAmountCents,
    slidingScaleAttestation,
    baseUrl,
  } = opts;

  if (!stripeCustomerId && !customerEmail) {
    throw new Error('harbor checkout needs a Stripe customer id or an email');
  }

  if (
    !Number.isInteger(monthlyAmountCents) ||
    monthlyAmountCents < HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS ||
    monthlyAmountCents > HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS
  ) {
    throw new Error(
      `harbor checkout amount out of band: ${monthlyAmountCents} cents (allowed [${HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS}, ${HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS}])`
    );
  }
  if (
    monthlyAmountCents < HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS &&
    !slidingScaleAttestation
  ) {
    throw new Error('harbor checkout below default requires sliding-scale attestation');
  }

  const metadata: Record<string, string> = {
    kind: 'harbor_make_public',
    build_id: buildId,
    monthly_amount_cents: String(monthlyAmountCents),
    sliding_scale_attestation: slidingScaleAttestation ? 'true' : 'false',
  };
  if (trialId) metadata.trial_id = trialId;
  if (keeperCustomerId) metadata.keeper_customer_id = keeperCustomerId;

  const session = await stripe.checkout.sessions.create({
    // Exactly one of the two: Stripe rejects a session that names both.
    ...(stripeCustomerId ? { customer: stripeCustomerId } : { customer_email: customerEmail }),
    mode: 'subscription',
    payment_method_types: ['card'],
    line_items: [
      {
        price_data: {
          currency: 'usd',
          product_data: {
            name: HARBOR_SUBSCRIPTION_PRODUCT_NAME,
            description: HARBOR_SUBSCRIPTION_PRODUCT_DESCRIPTION,
          },
          unit_amount: monthlyAmountCents,
          recurring: { interval: 'month' },
        },
        quantity: 1,
      },
    ],
    metadata,
    subscription_data: {
      metadata,
    },
    // No {CHECKOUT_SESSION_ID} on the return URL: nothing reads it. Who the
    // returning visitor is comes from their cookie (the session, or the trial
    // cookie that paid; see middleware/trial-cookie.ts), never from a value
    // in the address bar, which ends up in history and logs.
    success_url: `${baseUrl}/lighthouse/lamp/${buildId}/published`,
    cancel_url: `${baseUrl}/lighthouse/lamp/${buildId}/cancelled`,
  });

  if (!session.url) {
    throw new Error('Stripe harbor subscription session URL is null');
  }

  return session.url;
}

/**
 * Cancel-at-period-end the keeper's harbor subscription. The website
 * stays up; Stripe stops billing at the end of the current period.
 *
 * Returns the updated Stripe subscription object so the route handler
 * can mirror cancel_at_period_end into harbor_subscriptions for fast
 * reads without round-tripping Stripe on subsequent /lamp loads.
 */
export async function cancelHarborSubscriptionAtPeriodEnd(
  stripeSubscriptionId: string
): Promise<Stripe.Subscription> {
  if (!stripeSubscriptionId || !stripeSubscriptionId.startsWith('sub_')) {
    throw new Error(`invalid Stripe subscription id: ${stripeSubscriptionId}`);
  }
  return stripe.subscriptions.update(stripeSubscriptionId, {
    cancel_at_period_end: true,
  });
}

export function constructWebhookEvent(
  payload: Buffer,
  signature: string,
  secret: string
): Stripe.Event {
  return stripe.webhooks.constructEvent(payload, signature, secret);
}
