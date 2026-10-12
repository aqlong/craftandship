import { describe, it, expect } from 'vitest';
import crypto from 'crypto';
import Stripe from 'stripe';
import { constructWebhookEvent } from '../../src/services/stripe';

// constructWebhookEvent is the one place a Stripe webhook is authenticated.
// stripe.service.test.ts mocks the Stripe client; this file uses the real
// library and real HMAC signatures (no network), so a body that was changed
// after signing, a wrong secret and a stale timestamp are proven to be refused.

// Built at runtime so no secret-shaped literal sits in the repository.
const secret = ['whsec', crypto.randomBytes(24).toString('hex')].join('_');
const otherSecret = ['whsec', crypto.randomBytes(24).toString('hex')].join('_');
const payload = JSON.stringify({ id: 'evt_signature_test', object: 'event', type: 'checkout.session.completed', data: { object: {} } });
const helper = new Stripe('', { apiVersion: '2023-10-16' });

function header(body: string, withSecret: string, timestamp?: number): string {
  return helper.webhooks.generateTestHeaderString({ payload: body, secret: withSecret, timestamp });
}

describe('constructWebhookEvent (real signature verification)', () => {
  it('accepts a body signed with the right secret and returns the event', () => {
    const event = constructWebhookEvent(Buffer.from(payload), header(payload, secret), secret);
    expect(event.id).toBe('evt_signature_test');
    expect(event.type).toBe('checkout.session.completed');
  });

  it('refuses a body that was changed after it was signed', () => {
    const signature = header(payload, secret);
    const tampered = payload.replace('checkout.session.completed', 'checkout.session.expired');
    expect(() => constructWebhookEvent(Buffer.from(tampered), signature, secret)).toThrow(/signature/i);
  });

  it('refuses a signature made with a different secret', () => {
    expect(() => constructWebhookEvent(Buffer.from(payload), header(payload, otherSecret), secret)).toThrow(/signature/i);
  });

  it('refuses a correctly signed delivery whose timestamp is outside the tolerance (replay)', () => {
    const tenMinutesAgo = Math.floor(Date.now() / 1000) - 600;
    expect(() => constructWebhookEvent(Buffer.from(payload), header(payload, secret, tenMinutesAgo), secret)).toThrow(/tolerance|timestamp/i);
  });

  it('refuses a malformed header', () => {
    expect(() => constructWebhookEvent(Buffer.from(payload), 'not-a-stripe-signature-header', secret)).toThrow();
  });
});
