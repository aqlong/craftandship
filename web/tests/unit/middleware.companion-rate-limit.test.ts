import { describe, it, expect } from 'vitest';
import express from 'express';
import request from 'supertest';
import {
  companionTurnRateLimit,
  companionTurnKeyGen,
  companionIpKeyGen,
  makeCompanionTurnRateLimit,
  rateLimitIpKey,
} from '../../src/middleware/companion-rate-limit';

/**
 * Tests for the companion turn rate limiter
 * (harbor-companion-turn-rate-limit-and-cost-cap, 2026-05-09).
 *
 * The default singleton has a 10_000 ceiling under NODE_ENV=test so
 * normal app tests aren't throttled. To exercise the cap we use the
 * factory `makeCompanionTurnRateLimit({max:N})` which builds an
 * isolated limiter with our chosen cap.
 */

describe('companionTurnKeyGen', () => {
  it('keys on trial cookie when present', () => {
    const req = {
      ip: '1.2.3.4',
      harborTrial: { trial_id: 'trial-abc' },
    } as unknown as express.Request;
    expect(companionTurnKeyGen(req)).toBe('trial:trial-abc');
  });

  it('falls back to IP when no trial is attached', () => {
    const req = { ip: '1.2.3.4' } as unknown as express.Request;
    expect(companionTurnKeyGen(req)).toBe('ip:1.2.3.4');
  });

  it('uses "unknown" placeholder when neither trial nor IP is present', () => {
    const req = {} as unknown as express.Request;
    expect(companionTurnKeyGen(req)).toBe('ip:unknown');
  });
});

describe('companionTurnRateLimit middleware (factory)', () => {
  function appWithLimiter(
    limiter: express.RequestHandler,
    trialId: () => string | null = () => 'trial-1'
  ): express.Express {
    const app = express();
    app.use(express.json());
    app.use((req, _res, next) => {
      const t = trialId();
      if (t) {
        (req as express.Request & {
          harborTrial?: { trial_id: string };
        }).harborTrial = { trial_id: t };
      }
      next();
    });
    app.post('/turn', limiter, (_req, res) => {
      res.json({ ok: true });
    });
    return app;
  }

  it('allows requests under the limit + sets standard rate-limit headers', async () => {
    const limiter = makeCompanionTurnRateLimit({ max: 5, windowMs: 60_000 });
    const app = appWithLimiter(limiter);
    const res = await request(app).post('/turn').send({});
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-limit']).toBeTruthy();
    expect(res.headers['ratelimit-remaining']).toBeTruthy();
  });

  it('returns 429 with rate_limited body after exceeding the cap', async () => {
    const limiter = makeCompanionTurnRateLimit({ max: 2, windowMs: 60_000 });
    const app = appWithLimiter(limiter);
    expect((await request(app).post('/turn').send({})).status).toBe(200);
    expect((await request(app).post('/turn').send({})).status).toBe(200);
    const r3 = await request(app).post('/turn').send({});
    expect(r3.status).toBe(429);
    expect(r3.body.error).toBe('rate_limited');
    expect(r3.body.message).toMatch(/pause for a moment/i);
  });

  it('keys per-trial-cookie so two trials get separate buckets', async () => {
    const limiter = makeCompanionTurnRateLimit({ max: 1, windowMs: 60_000 });
    let trialId: string | null = 'trial-A';
    const app = appWithLimiter(limiter, () => trialId);
    expect((await request(app).post('/turn').send({})).status).toBe(200);
    expect((await request(app).post('/turn').send({})).status).toBe(429);
    trialId = 'trial-B';
    expect((await request(app).post('/turn').send({})).status).toBe(200);
  });

  it('falls back to IP key when no trial cookie is present', async () => {
    const limiter = makeCompanionTurnRateLimit({ max: 2, windowMs: 60_000 });
    const app = appWithLimiter(limiter, () => null);
    expect((await request(app).post('/turn').send({})).status).toBe(200);
    expect((await request(app).post('/turn').send({})).status).toBe(200);
    const r3 = await request(app).post('/turn').send({});
    expect(r3.status).toBe(429);
  });

  it('default singleton in test mode does not throttle (10_000 ceiling)', async () => {
    const app = appWithLimiter(companionTurnRateLimit);
    for (let i = 0; i < 5; i++) {
      const r = await request(app).post('/turn').send({});
      expect(r.status).toBe(200);
    }
  });
});

describe('rateLimitIpKey (R1-W2 review)', () => {
  it('uses IPv4 addresses as-is', () => {
    expect(rateLimitIpKey('203.0.113.9')).toBe('ip:203.0.113.9');
  });

  it('unwraps IPv4-mapped IPv6 so both spellings share a bucket', () => {
    expect(rateLimitIpKey('::ffff:203.0.113.9')).toBe('ip:203.0.113.9');
  });

  it('collapses an IPv6 address to its /64 so a subscriber cannot rotate within it', () => {
    const a = rateLimitIpKey('2001:db8:abcd:12:aaaa:bbbb:cccc:dddd');
    const b = rateLimitIpKey('2001:db8:abcd:12::1');
    const c = rateLimitIpKey('2001:0db8:ABCD:0012:0:0:0:ffff');
    expect(a).toBe('ip6:2001:db8:abcd:12');
    expect(b).toBe(a);
    expect(c).toBe(a);
    expect(rateLimitIpKey('2001:db8:abcd:13::1')).not.toBe(a);
  });

  it('handles the loopback and unspecified forms and missing input', () => {
    expect(rateLimitIpKey('::1')).toBe('ip6:0:0:0:0');
    expect(rateLimitIpKey(undefined)).toBe('ip:unknown');
    expect(rateLimitIpKey('')).toBe('ip:unknown');
  });
});

describe('per-address limiter bounds a client that discards its trial cookie (R1-W2 review)', () => {
  function appWithBothLimiters(opts: {
    perTrialMax: number;
    perIpMax: number;
  }): express.Express {
    const app = express();
    app.use(express.json());
    // Simulate the trial-cookie middleware for a cookie-less client: EVERY
    // request is handed a brand-new trial id, exactly like mintTrial().
    let n = 0;
    app.use((req, _res, next) => {
      n += 1;
      (req as express.Request & { harborTrial?: { trial_id: string } }).harborTrial = {
        trial_id: `fresh-trial-${n}`,
      };
      next();
    });
    app.post(
      '/turn',
      makeCompanionTurnRateLimit({
        max: opts.perIpMax,
        windowMs: 60_000,
        keyGenerator: companionIpKeyGen,
      }),
      makeCompanionTurnRateLimit({ max: opts.perTrialMax, windowMs: 60_000 }),
      (_req, res) => {
        res.json({ ok: true });
      }
    );
    return app;
  }

  it('the per-trial limiter alone never trips for a cookie-less client', async () => {
    const app = express();
    app.use(express.json());
    let n = 0;
    app.use((req, _res, next) => {
      n += 1;
      (req as express.Request & { harborTrial?: { trial_id: string } }).harborTrial = {
        trial_id: `fresh-trial-${n}`,
      };
      next();
    });
    app.post('/turn', makeCompanionTurnRateLimit({ max: 1, windowMs: 60_000 }), (_req, res) => {
      res.json({ ok: true });
    });
    for (let i = 0; i < 5; i++) {
      expect((await request(app).post('/turn').send({})).status).toBe(200);
    }
  });

  it('adding the per-address limiter stops the same client at its cap', async () => {
    const app = appWithBothLimiters({ perTrialMax: 1, perIpMax: 3 });
    const statuses: number[] = [];
    for (let i = 0; i < 5; i++) {
      statuses.push((await request(app).post('/turn').send({})).status);
    }
    expect(statuses).toEqual([200, 200, 200, 429, 429]);
  });

  it('the build limiter message does not say "messages"', async () => {
    const limiter = makeCompanionTurnRateLimit({
      max: 1,
      windowMs: 60_000,
      message: 'That was a lot of build attempts in a short time.',
    });
    const app = express();
    app.post('/build', limiter, (_req, res) => {
      res.json({ ok: true });
    });
    await request(app).post('/build');
    const r2 = await request(app).post('/build');
    expect(r2.status).toBe(429);
    expect(r2.body.message).toMatch(/build attempts/);
  });
});
