import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';

const mockSessionsCreate = vi.hoisted(() => vi.fn());
const mockSubscriptionsUpdate = vi.hoisted(() => vi.fn());
const mockCustomersCreate = vi.hoisted(() => vi.fn());
/** The options each Stripe client was constructed with. */
const stripeClientOptions = vi.hoisted(() => [] as Array<Record<string, unknown>>);

vi.mock('stripe', () => {
  // Must use a regular function (not arrow) to be a valid constructor
  function StripeMock(_key: string, options: Record<string, unknown>) {
    stripeClientOptions.push(options);
    return {
      checkout: {
        sessions: {
          create: mockSessionsCreate,
          retrieve: vi.fn(),
        },
      },
      customers: {
        create: mockCustomersCreate,
      },
      subscriptions: {
        update: mockSubscriptionsUpdate,
      },
      webhooks: {
        constructEvent: vi.fn(),
      },
    };
  }
  return { default: StripeMock };
});

// `createCheckoutSession` (legacy one-time tier-based payment) was removed
// 2026-05-01 with the tier kill. The describe block for it lived here; all
// remaining cases below cover the Harbor subscription flow exclusively.

import {
  createHarborSubscriptionCheckoutSession,
  cancelHarborSubscriptionAtPeriodEnd,
  HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS,
  HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS,
  HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS,
} from '../../src/services/stripe';

describe('createHarborSubscriptionCheckoutSession', () => {
  beforeEach(() => {
    mockSessionsCreate.mockReset();
  });

  const baseOpts = {
    buildId: 'build-uuid-1',
    trialId: 'trial-uuid-1',
    keeperCustomerId: null as string | null,
    stripeCustomerId: 'cus_harbor1' as string | null,
    customerEmail: 'payer@example.com',
    monthlyAmountCents: HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS,
    slidingScaleAttestation: false,
    baseUrl: 'https://craftandship.io',
  };

  it('uses subscription mode with monthly recurring price_data', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession(baseOpts);

    expect(mockSessionsCreate).toHaveBeenCalledOnce();
    const params = mockSessionsCreate.mock.calls[0][0];
    expect(params.mode).toBe('subscription');
    expect(params.line_items[0].price_data.unit_amount).toBe(HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS);
    expect(params.line_items[0].price_data.currency).toBe('usd');
    expect(params.line_items[0].price_data.recurring).toEqual({ interval: 'month' });
    expect(params.line_items[0].quantity).toBe(1);
  });

  it('passes harbor_make_public metadata + mirrors into subscription_data.metadata', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession({
      ...baseOpts,
      monthlyAmountCents: 2900,
      slidingScaleAttestation: true,
    });

    const params = mockSessionsCreate.mock.calls[0][0];
    expect(params.metadata.kind).toBe('harbor_make_public');
    expect(params.metadata.build_id).toBe('build-uuid-1');
    expect(params.metadata.trial_id).toBe('trial-uuid-1');
    expect(params.metadata.monthly_amount_cents).toBe('2900');
    expect(params.metadata.sliding_scale_attestation).toBe('true');
    expect(params.subscription_data.metadata).toEqual(params.metadata);
  });

  it('includes keeper_customer_id when provided (post-promotion path)', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession({
      ...baseOpts,
      trialId: null,
      keeperCustomerId: 'keeper-uuid-1',
    });

    const params = mockSessionsCreate.mock.calls[0][0];
    expect(params.metadata.keeper_customer_id).toBe('keeper-uuid-1');
    expect(params.metadata.trial_id).toBeUndefined();
  });

  it('builds success_url and cancel_url against the build, with no session id in either', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession({
      ...baseOpts,
      buildId: 'build-xyz',
      baseUrl: 'http://localhost:4000',
    });

    const params = mockSessionsCreate.mock.calls[0][0];
    // Who the returning visitor is comes from their cookie, never from the
    // address bar, so the Checkout session id is not put there.
    expect(params.success_url).toBe('http://localhost:4000/lighthouse/lamp/build-xyz/published');
    expect(params.success_url).not.toContain('CHECKOUT_SESSION_ID');
    expect(params.cancel_url).toBe('http://localhost:4000/lighthouse/lamp/build-xyz/cancelled');
  });

  it('rejects amounts below the sliding-scale floor', async () => {
    await expect(
      createHarborSubscriptionCheckoutSession({
        ...baseOpts,
        monthlyAmountCents: HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS - 1,
        slidingScaleAttestation: true,
      })
    ).rejects.toThrow(/out of band/);
    expect(mockSessionsCreate).not.toHaveBeenCalled();
  });

  it('rejects amounts above the standard price', async () => {
    await expect(
      createHarborSubscriptionCheckoutSession({
        ...baseOpts,
        monthlyAmountCents: HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS + 1,
      })
    ).rejects.toThrow(/out of band/);
    expect(mockSessionsCreate).not.toHaveBeenCalled();
  });

  it('rejects non-integer amounts', async () => {
    await expect(
      createHarborSubscriptionCheckoutSession({
        ...baseOpts,
        monthlyAmountCents: 2999.5,
      })
    ).rejects.toThrow(/out of band/);
  });

  it('rejects below-default amount without sliding-scale attestation', async () => {
    await expect(
      createHarborSubscriptionCheckoutSession({
        ...baseOpts,
        monthlyAmountCents: 2900,
        slidingScaleAttestation: false,
      })
    ).rejects.toThrow(/sliding-scale attestation/);
    expect(mockSessionsCreate).not.toHaveBeenCalled();
  });

  it('allows default amount without attestation', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession({
      ...baseOpts,
      monthlyAmountCents: HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS,
      slidingScaleAttestation: false,
    });

    expect(mockSessionsCreate).toHaveBeenCalledOnce();
  });

  it('throws when Stripe returns null URL', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: null });

    await expect(
      createHarborSubscriptionCheckoutSession(baseOpts)
    ).rejects.toThrow(/harbor subscription session URL is null/);
  });

  it('a Stripe customer on file is passed as `customer`, and no email with it', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession({ ...baseOpts, stripeCustomerId: 'cus_unique' });

    const params = mockSessionsCreate.mock.calls[0][0];
    expect(params.customer).toBe('cus_unique');
    // Stripe refuses a session that names both.
    expect(params).not.toHaveProperty('customer_email');
  });

  it('with no Stripe customer on file, Checkout is given the email and creates the customer itself when paid', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession({ ...baseOpts, stripeCustomerId: null, customerEmail: 'lena@example.com' });

    const params = mockSessionsCreate.mock.calls[0][0];
    expect(params.customer_email).toBe('lena@example.com');
    expect(params).not.toHaveProperty('customer');
    // Nothing else was created at Stripe for this request.
    expect(mockCustomersCreate).not.toHaveBeenCalled();
  });

  it('refuses a session with neither a customer nor an email', async () => {
    await expect(
      createHarborSubscriptionCheckoutSession({ ...baseOpts, stripeCustomerId: null, customerEmail: '' })
    ).rejects.toThrow(/needs a Stripe customer id or an email/);
    expect(mockSessionsCreate).not.toHaveBeenCalled();
  });

  it('product name does not contain em dashes (brand voice)', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });

    await createHarborSubscriptionCheckoutSession(baseOpts);

    const params = mockSessionsCreate.mock.calls[0][0];
    const name = params.line_items[0].price_data.product_data.name;
    const description = params.line_items[0].price_data.product_data.description;
    expect(name).not.toMatch(/\u2014/);
    expect(description).not.toMatch(/\u2014/);
  });

  // What the payer reads on Stripe's Checkout page and receipts is customer
  // copy like any other: no internal names for the product or the customer,
  // no reviewer named, and nothing the terms page does not also promise.
  it('the product name and description sent to Stripe use no internal vocabulary and name no reviewer', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });
    await createHarborSubscriptionCheckoutSession(baseOpts);
    const product = mockSessionsCreate.mock.calls[0][0].line_items[0].price_data.product_data as { name: string; description: string };

    expect(product.name).toBe('Craft & Ship website subscription');
    for (const text of [product.name, product.description]) {
      expect(text).not.toMatch(/keeper|harbor|lighthouse/i);
      expect(text).not.toMatch(/senior|engineer|craftsman|personally|by hand/i);
    }
    expect(product.description).toContain('Haven, your AI companion');
  });

  it('the description promises what the terms page promises for the subscription, in the same words', async () => {
    mockSessionsCreate.mockResolvedValueOnce({ url: 'https://checkout.stripe.com/sub_test' });
    await createHarborSubscriptionCheckoutSession(baseOpts);
    const description = mockSessionsCreate.mock.calls[0][0].line_items[0].price_data.product_data.description as string;
    const terms = fs
      .readFileSync(path.join(__dirname, '../../views/terms.ejs'), 'utf-8')
      .replace(/<%[\s\S]*?%>/g, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ');
    for (const promise of ['hosting', 'a recorded review approval for every website before it goes live', 'unlimited revisions and rebuilds']) {
      expect(description, promise).toContain(promise);
      expect(terms, promise).toContain(promise);
    }
    // The website is not taken down by cancelling, in the terms' words and in these.
    expect(description).toContain('Your website stays up at its address.');
    expect(terms).toContain('cancelling does not take your website down');
  });
});

describe('the Stripe client has a deadline', () => {
  it('each call is given 20 seconds and one retry, not the SDK\'s 80 seconds: the caller is a visitor\'s request', () => {
    // The module builds one client when it is loaded.
    expect(stripeClientOptions).toHaveLength(1);
    expect(stripeClientOptions[0]).toMatchObject({ timeout: 20_000, maxNetworkRetries: 1 });
  });
});

describe('services/stripe exports', () => {
  it('exports only what the application calls', async () => {
    const mod = await import('../../src/services/stripe');
    expect(Object.keys(mod).sort()).toEqual([
      'HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS',
      'HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS',
      'HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS',
      'cancelHarborSubscriptionAtPeriodEnd',
      'constructWebhookEvent',
      'createHarborSubscriptionCheckoutSession',
    ]);
  });
});

describe('cancelHarborSubscriptionAtPeriodEnd', () => {
  beforeEach(() => {
    mockSubscriptionsUpdate.mockReset();
  });

  it('calls stripe.subscriptions.update with cancel_at_period_end: true', async () => {
    mockSubscriptionsUpdate.mockResolvedValueOnce({ id: 'sub_x', cancel_at_period_end: true });
    const result = await cancelHarborSubscriptionAtPeriodEnd('sub_x');
    expect(mockSubscriptionsUpdate).toHaveBeenCalledOnce();
    expect(mockSubscriptionsUpdate.mock.calls[0]).toEqual(['sub_x', { cancel_at_period_end: true }]);
    expect(result).toEqual({ id: 'sub_x', cancel_at_period_end: true });
  });

  it('rejects malformed subscription id', async () => {
    await expect(cancelHarborSubscriptionAtPeriodEnd('')).rejects.toThrow(/invalid Stripe/);
    await expect(cancelHarborSubscriptionAtPeriodEnd('cus_wrong_prefix')).rejects.toThrow(/invalid Stripe/);
    expect(mockSubscriptionsUpdate).not.toHaveBeenCalled();
  });
});
