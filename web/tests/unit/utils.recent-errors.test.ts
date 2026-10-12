import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';

// The recent-error buffer behind GET /ops/api/v1/errors. It lives in
// src/utils/recent-errors.ts so the ops router does not import the app.

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }),
    connect: vi.fn(),
    on: vi.fn(),
  },
}));

import app from '../../src/app';
import pool from '../../src/db/index';
import { getRecentErrors, recordRecentError } from '../../src/utils/recent-errors';

const entry = (n: number) => ({ timestamp: new Date(0).toISOString(), method: 'GET', path: `/p${n}`, message: `m${n}` });

describe('recordRecentError', () => {
  beforeEach(() => {
    getRecentErrors().length = 0;
  });

  it('keeps only the 20 most recent entries, oldest dropped first', () => {
    for (let i = 1; i <= 25; i++) recordRecentError(entry(i));
    const kept = getRecentErrors();
    expect(kept).toHaveLength(20);
    expect(kept[0].path).toBe('/p6');
    expect(kept[19].path).toBe('/p25');
  });
});

describe('GET /ops/api/v1/errors after a real unhandled error', () => {
  const opsKey = ['ops', 'errors', 'key'].join('-');
  const original = process.env.OPS_API_KEY;

  beforeEach(() => {
    getRecentErrors().length = 0;
    process.env.OPS_API_KEY = opsKey;
  });

  afterEach(() => {
    if (original === undefined) delete process.env.OPS_API_KEY;
    else process.env.OPS_API_KEY = original;
  });

  it('lists the request that failed, with its method and path', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    // POST /v1/builds/cleanup-abandoned awaits pool.connect() before its try block.
    (pool as unknown as { connect: ReturnType<typeof vi.fn> }).connect.mockRejectedValueOnce(new Error('pool exhausted'));
    try {
      const failed = await request(app)
        .post('/ops/api/v1/builds/cleanup-abandoned')
        .set('Authorization', `Bearer ${opsKey}`);
      expect(failed.status).toBe(500);

      const res = await request(app).get('/ops/api/v1/errors').set('Authorization', `Bearer ${opsKey}`);
      expect(res.status).toBe(200);
      expect(res.body.errors).toHaveLength(1);
      expect(res.body.errors[0]).toMatchObject({
        method: 'POST',
        path: '/ops/api/v1/builds/cleanup-abandoned',
        message: 'pool exhausted',
      });
    } finally {
      errorSpy.mockRestore();
    }
  });
});
