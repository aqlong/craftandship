// The admin delete of a build, through the real app, the real Railway client
// and a real Postgres. The Railway API is a stand-in behind `fetch`: it answers
// the project-name read and the projectDelete mutation from the values each
// test sets, and every other outbound request is refused.
//
// What each case leaves behind is read back from the builds table: the row is
// removed only when the project's name matches the build and Railway either
// deletes the project or says with the NOT_FOUND code that it is already gone.
//
// It runs only when CAS_SCHEMA_TEST_DATABASE_URL points at a THROWAWAY
// database (the same switch as the other *.realdb.test.ts files; run them with
// --no-file-parallelism, because each applies schema.sql).

import { describe, it, expect, vi, beforeAll, afterAll, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import request from 'supertest';
import bcrypt from 'bcrypt';
import { Client } from 'pg';
import type { Application, Request } from 'express';

const integrationUrl = process.env.CAS_SCHEMA_TEST_DATABASE_URL;
const schema = fs.readFileSync(path.join(__dirname, '../../src/db/schema.sql'), 'utf-8');

const ADMIN_EMAIL = 'admin-delete-realdb@example.com';
// Built at runtime: the value is only a test password for the admin row below.
const ADMIN_PASSWORD = ['admin', 'delete', 'realdb'].join('-');
const PROJECT_ID = '00000000-0000-4000-8000-0000000000d1';

let app: Application;
let appPool: { end: () => Promise<void> };
let db: Client;

/** What the stand-in Railway answers. Each test sets the bodies it needs. */
const railway = {
  readBody: (): unknown => ({ data: { project: { name: 'sample-studio' } } }),
  deleteBody: (): unknown => ({ data: { projectDelete: true } }),
  calls: [] as string[],
};

function fakeRailwayFetch(url: unknown, init?: RequestInit): Promise<Response> {
  const target = String(url);
  if (target.startsWith('https://backboard.railway.com/graphql/v2')) {
    const query = (JSON.parse(String(init?.body)) as { query: string }).query;
    const isDelete = query.startsWith('mutation');
    railway.calls.push(isDelete ? 'projectDelete' : 'project');
    const payload = isDelete ? railway.deleteBody() : railway.readBody();
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
  }
  return Promise.reject(new Error(`routes.admin-delete.realdb.test.ts: outbound fetch to ${target} blocked`));
}

async function applySchema(client: Client): Promise<void> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      await client.query('BEGIN');
      await client.query(schema);
      await client.query('COMMIT');
      return;
    } catch (err) {
      await client.query('ROLLBACK').catch(() => undefined);
      if (attempt >= 5) throw err;
      await new Promise((resolve) => setTimeout(resolve, 300 * attempt));
    }
  }
}

async function seedBuild(appName: string | null): Promise<string> {
  const owner = await db.query<{ id: string }>(
    `INSERT INTO customers (email, password, name, email_verified)
     VALUES ($1, 'unused-hash', 'Owner', TRUE)
     RETURNING id`,
    [`owner-${Math.random().toString(36).slice(2)}@example.com`],
  );
  const build = await db.query<{ id: string }>(
    `INSERT INTO builds (customer_id, tier, status, app_name, railway_project_id)
     VALUES ($1, 'build', 'preview', $2, $3)
     RETURNING id`,
    [owner.rows[0].id, appName, PROJECT_ID],
  );
  return build.rows[0].id;
}

async function buildRowCount(buildId: string): Promise<number> {
  const res = await db.query<{ n: number }>('SELECT COUNT(*)::int AS n FROM builds WHERE id = $1', [buildId]);
  return res.rows[0].n;
}

async function adminAgent(): Promise<ReturnType<typeof request.agent>> {
  const agent = request.agent(app);
  const csrf = (await import('../../src/middleware/auth')).generateCsrfToken({} as Request);
  const res = await agent.post('/login').type('form').send({ _csrf: csrf, email: ADMIN_EMAIL, password: ADMIN_PASSWORD });
  expect(res.status).toBe(302);
  return agent;
}

/** The delete request, with Railway writes switched on for that request only (the route needs production). */
async function deleteBuild(buildId: string): Promise<{ status: number; text: string }> {
  const agent = await adminAgent();
  const csrf = (await import('../../src/middleware/auth')).generateCsrfToken({} as Request);
  vi.stubEnv('NODE_ENV', 'production');
  vi.stubEnv('CAS_ALLOW_RAILWAY_WRITES', 'true');
  try {
    const res = await agent.post(`/admin/builds/${buildId}/delete`).type('form').send({ _csrf: csrf });
    return { status: res.status, text: res.text };
  } finally {
    vi.unstubAllEnvs();
  }
}

describe.skipIf(!integrationUrl)('admin build delete, real app and real Postgres (integration)', () => {
  beforeAll(async () => {
    db = new Client({ connectionString: integrationUrl });
    await db.connect();
    await applySchema(db);
    const hash = await bcrypt.hash(ADMIN_PASSWORD, 4);
    await db.query(
      `INSERT INTO customers (email, password, name, email_verified)
       VALUES ($1, $2, 'Admin', TRUE)
       ON CONFLICT (email) DO UPDATE SET password = EXCLUDED.password`,
      [ADMIN_EMAIL, hash],
    );

    process.env.DATABASE_URL = integrationUrl;
    process.env.ADMIN_EMAIL = ADMIN_EMAIL;
    process.env.RAILWAY_API_TOKEN = ['railway', 'test', 'token'].join('-');
    delete process.env.RESEND_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    delete process.env.PROTECTED_RAILWAY_PROJECT_IDS;
    delete process.env.RAILWAY_PROJECT_ID;
    vi.resetModules();
    vi.stubGlobal('fetch', fakeRailwayFetch);
    for (const level of ['log', 'info', 'warn', 'error'] as const) vi.spyOn(console, level).mockImplementation(() => undefined);
    app = (await import('../../src/app')).default;
    appPool = (await import('../../src/db/index')).default as unknown as { end: () => Promise<void> };
  }, 60_000);

  afterAll(async () => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    if (appPool) await appPool.end();
    if (db) {
      await db.query('DELETE FROM builds WHERE railway_project_id = $1', [PROJECT_ID]).catch(() => undefined);
      await db.end();
    }
  });

  beforeEach(() => {
    railway.readBody = (): unknown => ({ data: { project: { name: 'sample-studio' } } });
    railway.deleteBody = (): unknown => ({ data: { projectDelete: true } });
    railway.calls = [];
  });

  it('deletes the row when the name read back matches the build and Railway deletes the project', async () => {
    const buildId = await seedBuild('Sample Studio');
    const res = await deleteBuild(buildId);
    expect(res.status).toBe(302);
    expect(railway.calls).toEqual(['project', 'projectDelete']);
    expect(await buildRowCount(buildId)).toBe(0);
  });

  it('keeps the row when Railway says "not found" without the NOT_FOUND code on the delete', async () => {
    const buildId = await seedBuild('sample-studio');
    railway.deleteBody = (): unknown => ({
      errors: [{ message: 'Project not found or you do not have access' }],
    });
    const res = await deleteBuild(buildId);
    expect(res.status).toBe(502);
    expect(await buildRowCount(buildId)).toBe(1);
  });

  it('keeps the row and sends no delete when the name cannot be read because access is refused', async () => {
    const buildId = await seedBuild('sample-studio');
    railway.readBody = (): unknown => ({
      errors: [{ message: 'Project not found or you do not have access' }],
    });
    const res = await deleteBuild(buildId);
    expect(res.status).toBe(502);
    expect(railway.calls).toEqual(['project']);
    expect(await buildRowCount(buildId)).toBe(1);
  });

  it('deletes the row when the delete says NOT_FOUND and the name matched first', async () => {
    const buildId = await seedBuild('sample-studio');
    railway.deleteBody = (): unknown => ({
      errors: [{ message: 'Project not found', extensions: { code: 'NOT_FOUND' } }],
    });
    const res = await deleteBuild(buildId);
    expect(res.status).toBe(302);
    expect(railway.calls).toEqual(['project', 'projectDelete']);
    expect(await buildRowCount(buildId)).toBe(0);
  });

  it('keeps the project and the row when the project belongs to another name', async () => {
    const buildId = await seedBuild('sample-studio');
    railway.readBody = (): unknown => ({ data: { project: { name: 'another-customers-site' } } });
    const res = await deleteBuild(buildId);
    expect(res.status).toBe(502);
    expect(railway.calls).toEqual(['project']);
    expect(await buildRowCount(buildId)).toBe(1);
  });
});
