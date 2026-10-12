// The limiters that live next to their routes, through the real app.
//
// Two families of key: the customer id for routes behind a login (the chat
// limiters, feedback, logo upload), and the client address for routes anyone
// may call (the ops API before its key check, the contact and guide forms).
// The routes and limiters are real; the database is a stand-in. The app runs
// with one trusted proxy hop here, so a request can claim an address through
// X-Forwarded-For, which is also how text that is not an address gets in.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import supertest from 'supertest';
import bcrypt from 'bcrypt';
import type { Request } from 'express';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return { default: { query: mockQuery, connect: vi.fn(), on: vi.fn() } };
});

import app from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const SUPPORT_CHAT_PER_MINUTE = 10;
const OPS_PER_MINUTE = 60;

async function loggedInAgent(customerId: string, email: string): Promise<supertest.Agent> {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('a-test-password', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: customerId, email, name: 'Limiter User', password: hashed, email_verified: true }],
  });
  const login = await agent
    .post('/login')
    .type('form')
    .send({ _csrf: generateCsrfToken({} as Request), email, password: 'a-test-password' });
  expect(login.status).toBe(302);
  return agent;
}

describe('logged-in family: the support chat limiter is keyed on the customer', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [], rowCount: 0 });
  });

  // No CSRF header is sent, so a request the limiter lets through is refused
  // by the next middleware (403). What matters here is when 429 starts.
  const chat = (agent: supertest.Agent, address: string) =>
    agent.post('/api/support/chat').set('X-Forwarded-For', address).send({ message: 'hello' });

  it('one customer gets one budget on every address; another customer on the same address has their own', async () => {
    const alice = await loggedInAgent('customer-limiter-alice', 'alice-limiter@example.com');
    const bob = await loggedInAgent('customer-limiter-bob', 'bob-limiter@example.com');

    for (let i = 0; i < SUPPORT_CHAT_PER_MINUTE; i += 1) {
      expect((await chat(alice, '198.51.100.21')).status, `alice request ${i + 1}`).not.toBe(429);
    }
    const refused = await chat(alice, '198.51.100.21');
    expect(refused.status).toBe(429);
    expect(refused.body.error).toMatch(/Too many messages/);

    // A new address does not buy her a new budget.
    expect((await chat(alice, '198.51.100.22')).status).toBe(429);
    // Bob, behind the address Alice used, is not refused.
    expect((await chat(bob, '198.51.100.21')).status).not.toBe(429);
  });
});

describe('anonymous family: the ops API limiter is keyed on a validated address', () => {
  const original = process.env.OPS_API_KEY;
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [], rowCount: 0 });
    delete process.env.OPS_API_KEY;
    return () => {
      if (original !== undefined) process.env.OPS_API_KEY = original;
    };
  });

  const probe = (address: string) => supertest(app).get('/ops/api/v1/errors').set('X-Forwarded-For', address);

  it('text that is not an address cannot be rotated to get a fresh budget each time', async () => {
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    // 60 requests, each claiming a different "address" that is not one. With
    // no key configured the route itself answers 503; the limiter runs first.
    for (let i = 0; i < OPS_PER_MINUTE; i += 1) {
      expect((await probe(`forged-address-${i}`)).status, `request ${i + 1}`).toBe(503);
    }
    // They all counted against one bucket, so the next one is refused,
    const refused = await probe('forged-address-next');
    expect(refused.status).toBe(429);
    expect(refused.body.error).toBe('Rate limit exceeded.');
    // and a client with a real address is not caught up in it.
    expect((await probe('198.51.100.31')).status).toBe(503);
    vi.restoreAllMocks();
  });
});
