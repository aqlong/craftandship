// TLS selection for the Postgres pool (src/db/index.ts).
//
// The pool decides from DATABASE_URL and NODE_ENV whether to ask the server for
// TLS. A local server (and the Railway private network) does not offer it, so
// asking for TLS there fails every query; a remote server gets TLS without
// certificate verification unless NODE_ENV is exactly "development" or "test"
// (the app-wide rule, isDevOrTestEnv), as the comment in src/db/index.ts says.
//
// These tests import the real module with `pg` replaced by a recorder, so they
// assert the `ssl` option the pool is actually constructed with.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const poolOptions = vi.hoisted(() => [] as Array<Record<string, unknown>>);
vi.mock('pg', () => ({
  Pool: class {
    constructor(options: Record<string, unknown>) {
      poolOptions.push(options);
    }
    on(): void {}
    query(): void {}
    end(): void {}
  },
}));

const TOUCHED_ENV = ['DATABASE_URL', 'NODE_ENV', 'PGHOST'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of TOUCHED_ENV) savedEnv[key] = process.env[key];

/** `nodeEnv` undefined means NODE_ENV is not set at all. */
async function sslFor(databaseUrl: string, nodeEnv: string | undefined): Promise<unknown> {
  vi.resetModules();
  poolOptions.length = 0;
  process.env.DATABASE_URL = databaseUrl;
  if (nodeEnv === undefined) delete process.env.NODE_ENV;
  else process.env.NODE_ENV = nodeEnv;
  await import('../../src/db/index');
  expect(poolOptions).toHaveLength(1);
  return poolOptions[0].ssl;
}

const UNVERIFIED_TLS = { rejectUnauthorized: false };

beforeEach(() => {
  delete process.env.PGHOST;
});

afterEach(() => {
  for (const key of TOUCHED_ENV) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

describe('pool TLS in production', () => {
  // The first row is the form README.md tells a new reader to use.
  it.each([
    ['postgresql://localhost:5432/dbname'],
    ['postgres://localhost/dbname'],
    ['postgresql://app:pw@localhost:5432/dbname'],
    ['postgresql://127.0.0.1:5432/dbname'],
    ['postgresql://app:pw@127.0.0.1:5432/dbname'],
    ['postgresql://[::1]:5432/dbname'],
    ['postgresql://app:pw@[::1]:5432/dbname'],
    ['postgresql://LOCALHOST/dbname'],
    ['postgresql:///dbname'],
    ['postgresql://app@/dbname?host=/var/run/postgresql'],
    ['postgresql://%2Fvar%2Frun%2Fpostgresql/dbname'],
    ['postgresql://db.internal-name.railway.internal:5432/railway'],
    ['postgresql://app:pw@postgres.railway.internal:5432/railway'],
    // The driver lets a ?host= parameter override the host in the authority.
    ['postgresql://app:pw@db.example.com/dbname?host=localhost'],
    ['postgresql://app:p@ss@localhost/dbname'],
    ['/var/run/postgresql'],
  ])('does not ask %s for TLS', async (url) => {
    expect(await sslFor(url, 'production')).toBe(false);
  });

  it.each([
    ['postgresql://app:pw@db.example.com:5432/dbname'],
    ['postgresql://db.example.com/dbname'],
    ['postgresql://app:pw@10.0.0.5:5432/dbname'],
    // The host decides, not a look-alike elsewhere in the string.
    ['postgresql://app:pw@db.example.com/dbname?note=localhost'],
    ['postgresql://localhost:pw@db.example.com/dbname'],
    ['postgresql://app:pw@localhost.example.com/dbname'],
    ['postgresql://app:pw@db.example.com/localhost'],
    ['postgresql://app:pw@db.example.com/dbname?options=railway.internal'],
    ['postgresql://app:pw@railway.internal.example.com/dbname'],
    // A password that looks like a private-network host (assembled at runtime).
    [['postgresql://app:railway', '.internal@db.example.com/dbname'].join('')],
    ['postgresql://localhost/dbname?host=db.example.com'],
    ['postgresql://app:p@ss@db.example.com/dbname'],
    ['not a url'],
    [''],
  ])('asks %s for TLS without certificate verification', async (url) => {
    expect(await sslFor(url, 'production')).toEqual(UNVERIFIED_TLS);
  });
});

describe('pool TLS when the URL names no host', () => {
  it('follows PGHOST, as the driver does', async () => {
    process.env.PGHOST = 'db.example.com';
    expect(await sslFor('postgresql:///dbname', 'production')).toEqual(UNVERIFIED_TLS);
    process.env.PGHOST = '127.0.0.1';
    expect(await sslFor('postgresql:///dbname', 'production')).toBe(false);
  });
});

describe('pool TLS follows isDevOrTestEnv, not "is it production"', () => {
  const REMOTE = 'postgresql://app:pw@db.example.com:5432/dbname';
  const LOOPBACK = 'postgresql://localhost:5432/dbname';
  const RAILWAY_INTERNAL = 'postgresql://app:pw@postgres.railway.internal:5432/railway';

  // Only an explicit "development" or "test" counts as development. A
  // deployment that forgot NODE_ENV (unset, or an empty value) or runs as
  // "staging" gets the same TLS as "production".
  it.each([
    ['unset', undefined],
    ['empty', ''],
    ['staging', 'staging'],
    ['production', 'production'],
    ['Production (wrong case)', 'Production'],
  ])('NODE_ENV %s: a remote host gets TLS without certificate verification', async (_label, env) => {
    expect(await sslFor(REMOTE, env)).toEqual(UNVERIFIED_TLS);
  });

  it.each([
    ['development', 'development'],
    ['test', 'test'],
  ])('NODE_ENV %s: a remote host gets no TLS', async (_label, env) => {
    expect(await sslFor(REMOTE, env)).toBe(false);
  });

  it.each([
    ['unset', undefined],
    ['empty', ''],
    ['staging', 'staging'],
    ['production', 'production'],
    ['development', 'development'],
    ['test', 'test'],
  ])('NODE_ENV %s: a loopback host and a railway.internal host never get TLS', async (_label, env) => {
    expect(await sslFor(LOOPBACK, env)).toBe(false);
    expect(await sslFor(RAILWAY_INTERNAL, env)).toBe(false);
  });

  it('agrees with isDevOrTestEnv for every NODE_ENV value tried above', async () => {
    const { isDevOrTestEnv } = await import('../../src/middleware/auth');
    for (const env of [undefined, '', 'staging', 'production', 'Production', 'development', 'test']) {
      const asksForTls = (await sslFor(REMOTE, env)) !== false;
      expect(asksForTls, String(env)).toBe(!isDevOrTestEnv({ NODE_ENV: env }));
    }
  });
});
