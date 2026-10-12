import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { deleteRailwayProject, readRailwayProjectName, RailwayApiError, isRailwayWriteEnabled } from '../../src/services/railway';

function mockFetch(response: { status: number; body: unknown; ok?: boolean }): typeof fetch {
  return vi.fn().mockResolvedValue({
    ok: response.ok ?? response.status < 400,
    status: response.status,
    text: async () => (typeof response.body === 'string' ? response.body : JSON.stringify(response.body)),
  }) as unknown as typeof fetch;
}

describe('deleteRailwayProject', () => {
  const origNodeEnv = process.env.NODE_ENV;
  const origAllow = process.env.CAS_ALLOW_RAILWAY_WRITES;

  beforeEach(() => {
    process.env.RAILWAY_API_TOKEN = 'test-token';
    // Default: both signals present so happy-path tests still pass.
    process.env.NODE_ENV = 'production';
    process.env.CAS_ALLOW_RAILWAY_WRITES = 'true';
  });

  afterEach(() => {
    if (origNodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = origNodeEnv;
    if (origAllow === undefined) delete process.env.CAS_ALLOW_RAILWAY_WRITES;
    else process.env.CAS_ALLOW_RAILWAY_WRITES = origAllow;
  });

  it('posts the projectDelete mutation with a browser-like User-Agent', async () => {
    const fetchImpl = mockFetch({ status: 200, body: { data: { projectDelete: true } } });
    await deleteRailwayProject('proj-1', { fetchImpl });

    const call = (fetchImpl as any).mock.calls[0];
    expect(call[0]).toBe('https://backboard.railway.com/graphql/v2');
    expect(call[1].method).toBe('POST');
    expect(call[1].headers.Authorization).toBe('Bearer test-token');
    expect(call[1].headers['User-Agent']).toMatch(/Mozilla/);
    const body = JSON.parse(call[1].body);
    expect(body.query).toContain('projectDelete');
    expect(body.variables).toEqual({ id: 'proj-1' });
  });

  it('throws when RAILWAY_API_TOKEN is missing', async () => {
    delete process.env.RAILWAY_API_TOKEN;
    await expect(deleteRailwayProject('proj-1')).rejects.toBeInstanceOf(RailwayApiError);
  });

  it('treats a 404-style GraphQL error as alreadyGone', async () => {
    const fetchImpl = mockFetch({
      status: 200,
      body: { errors: [{ message: 'Project not found', extensions: { code: 'NOT_FOUND' } }] },
    });
    let caught: unknown;
    try {
      await deleteRailwayProject('proj-gone', { fetchImpl });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(RailwayApiError);
    expect((caught as RailwayApiError).alreadyGone).toBe(true);
  });

  it('throws a non-alreadyGone RailwayApiError on HTTP 500', async () => {
    const fetchImpl = mockFetch({ status: 500, body: 'upstream boom', ok: false });
    let caught: unknown;
    try {
      await deleteRailwayProject('proj-1', { fetchImpl });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(RailwayApiError);
    expect((caught as RailwayApiError).alreadyGone).toBe(false);
    expect((caught as RailwayApiError).status).toBe(500);
  });

  it('throws when projectDelete returns non-true', async () => {
    const fetchImpl = mockFetch({ status: 200, body: { data: { projectDelete: false } } });
    await expect(deleteRailwayProject('proj-1', { fetchImpl })).rejects.toBeInstanceOf(RailwayApiError);
  });

  it('wraps fetch transport errors in RailwayApiError', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('ECONNRESET')) as unknown as typeof fetch;
    let caught: unknown;
    try {
      await deleteRailwayProject('proj-1', { fetchImpl });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(RailwayApiError);
    expect((caught as Error).message).toContain('ECONNRESET');
  });

  it('refuses to call fetch when NODE_ENV is not production', async () => {
    process.env.NODE_ENV = 'development';
    const fetchImpl = vi.fn() as unknown as typeof fetch;
    let caught: unknown;
    try {
      await deleteRailwayProject('proj-1', { fetchImpl });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(RailwayApiError);
    expect((caught as RailwayApiError).disabled).toBe(true);
    expect(fetchImpl).not.toHaveBeenCalled();
    // The message must describe the real rule: both settings, not either one.
    const message = (caught as RailwayApiError).message;
    expect(message).toContain('both NODE_ENV=production and CAS_ALLOW_RAILWAY_WRITES=true');
    expect(message).not.toMatch(/NODE_ENV=production or /);
  });

  it('refuses to call fetch when CAS_ALLOW_RAILWAY_WRITES is unset', async () => {
    delete process.env.CAS_ALLOW_RAILWAY_WRITES;
    const fetchImpl = vi.fn() as unknown as typeof fetch;
    let caught: unknown;
    try {
      await deleteRailwayProject('proj-1', { fetchImpl });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(RailwayApiError);
    expect((caught as RailwayApiError).disabled).toBe(true);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('refuses even when NODE_ENV=production but flag says "false"', async () => {
    process.env.NODE_ENV = 'production';
    process.env.CAS_ALLOW_RAILWAY_WRITES = 'false';
    const fetchImpl = vi.fn() as unknown as typeof fetch;
    await expect(deleteRailwayProject('proj-1', { fetchImpl })).rejects.toBeInstanceOf(RailwayApiError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe('isRailwayWriteEnabled', () => {
  const origNodeEnv = process.env.NODE_ENV;
  const origAllow = process.env.CAS_ALLOW_RAILWAY_WRITES;

  afterEach(() => {
    if (origNodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = origNodeEnv;
    if (origAllow === undefined) delete process.env.CAS_ALLOW_RAILWAY_WRITES;
    else process.env.CAS_ALLOW_RAILWAY_WRITES = origAllow;
  });

  it('returns true only when BOTH NODE_ENV=production AND CAS_ALLOW_RAILWAY_WRITES=true', () => {
    process.env.NODE_ENV = 'production';
    process.env.CAS_ALLOW_RAILWAY_WRITES = 'true';
    expect(isRailwayWriteEnabled()).toBe(true);
  });

  it('returns false when NODE_ENV=production but flag missing', () => {
    process.env.NODE_ENV = 'production';
    delete process.env.CAS_ALLOW_RAILWAY_WRITES;
    expect(isRailwayWriteEnabled()).toBe(false);
  });

  it('returns false when flag=true but NODE_ENV is not production', () => {
    process.env.NODE_ENV = 'development';
    process.env.CAS_ALLOW_RAILWAY_WRITES = 'true';
    expect(isRailwayWriteEnabled()).toBe(false);
  });

  it('returns false when staging runs NODE_ENV=production without the flag', () => {
    process.env.NODE_ENV = 'production';
    delete process.env.CAS_ALLOW_RAILWAY_WRITES;
    expect(isRailwayWriteEnabled()).toBe(false);
  });

  it('returns false when flag is any string other than the literal "true"', () => {
    process.env.NODE_ENV = 'production';
    process.env.CAS_ALLOW_RAILWAY_WRITES = '1';
    expect(isRailwayWriteEnabled()).toBe(false);
    process.env.CAS_ALLOW_RAILWAY_WRITES = 'yes';
    expect(isRailwayWriteEnabled()).toBe(false);
    process.env.CAS_ALLOW_RAILWAY_WRITES = 'True';
    expect(isRailwayWriteEnabled()).toBe(false);
  });

  it('returns false when both unset', () => {
    delete process.env.NODE_ENV;
    delete process.env.CAS_ALLOW_RAILWAY_WRITES;
    expect(isRailwayWriteEnabled()).toBe(false);
  });
});

// Only the NOT_FOUND code means "already gone". The same words in a message do
// not: an access failure can say "Project not found or you do not have access".
describe('deleteRailwayProject: only the NOT_FOUND code means the project is gone', () => {
  beforeEach(() => {
    process.env.RAILWAY_API_TOKEN = 'test-token';
    process.env.NODE_ENV = 'production';
    process.env.CAS_ALLOW_RAILWAY_WRITES = 'true';
  });

  async function errorFrom(fetchImpl: typeof fetch): Promise<RailwayApiError> {
    try {
      await deleteRailwayProject('proj-1', { fetchImpl });
    } catch (err) {
      return err as RailwayApiError;
    }
    throw new Error('expected deleteRailwayProject to throw');
  }

  it('a GraphQL error worded "not found" without the code is not alreadyGone', async () => {
    const fetchImpl = mockFetch({
      status: 200,
      body: { errors: [{ message: 'Project not found or you do not have access' }] },
    });
    const err = await errorFrom(fetchImpl);
    expect(err).toBeInstanceOf(RailwayApiError);
    expect(err.alreadyGone).toBe(false);
  });

  it('an HTTP 404 whose body says "not found" without the code is not alreadyGone', async () => {
    const fetchImpl = mockFetch({
      status: 404,
      body: { errors: [{ message: 'Project not found' }] },
      ok: false,
    });
    const err = await errorFrom(fetchImpl);
    expect(err.alreadyGone).toBe(false);
    expect(err.status).toBe(404);
  });

  it('a "no project" message without the code is not alreadyGone', async () => {
    const fetchImpl = mockFetch({ status: 200, body: { errors: [{ message: 'no project with that id for this token' }] } });
    const err = await errorFrom(fetchImpl);
    expect(err.alreadyGone).toBe(false);
  });

  it('the NOT_FOUND code is alreadyGone even on an HTTP 404', async () => {
    const fetchImpl = mockFetch({
      status: 404,
      body: { errors: [{ message: 'Problem processing request', extensions: { code: 'NOT_FOUND' } }] },
      ok: false,
    });
    const err = await errorFrom(fetchImpl);
    expect(err.alreadyGone).toBe(true);
  });

  it('bounds the body read by the same timeout as the request', async () => {
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => ({
      ok: true,
      status: 200,
      text: () => new Promise<string>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new Error('aborted')));
      }),
    })) as unknown as typeof fetch;
    await expect(deleteRailwayProject('proj-1', { fetchImpl, timeoutMs: 20 })).rejects.toBeInstanceOf(RailwayApiError);
  });
});

describe('readRailwayProjectName', () => {
  beforeEach(() => {
    process.env.RAILWAY_API_TOKEN = 'test-token';
  });

  it('returns the name Railway gives the project, and queries only that field', async () => {
    const fetchImpl = mockFetch({ status: 200, body: { data: { project: { name: 'sample-studio' } } } });
    await expect(readRailwayProjectName('proj-1', { fetchImpl })).resolves.toBe('sample-studio');
    const body = JSON.parse((fetchImpl as any).mock.calls[0][1].body);
    expect(body.query).toContain('project(id: $id)');
    expect(body.variables).toEqual({ id: 'proj-1' });
  });

  it('throws alreadyGone only on the NOT_FOUND code', async () => {
    const gone = mockFetch({
      status: 200,
      body: { errors: [{ message: 'Project not found', extensions: { code: 'NOT_FOUND' } }] },
    });
    await expect(readRailwayProjectName('proj-1', { fetchImpl: gone })).rejects.toMatchObject({ alreadyGone: true });

    const access = mockFetch({ status: 200, body: { errors: [{ message: 'Project not found or you do not have access' }] } });
    await expect(readRailwayProjectName('proj-1', { fetchImpl: access })).rejects.toMatchObject({ alreadyGone: false });
  });

  it('throws when the reply carries no name, and when the request fails', async () => {
    const noName = mockFetch({ status: 200, body: { data: { project: null } } });
    await expect(readRailwayProjectName('proj-1', { fetchImpl: noName })).rejects.toBeInstanceOf(RailwayApiError);

    const failing = mockFetch({ status: 500, body: 'upstream boom', ok: false });
    await expect(readRailwayProjectName('proj-1', { fetchImpl: failing })).rejects.toMatchObject({ alreadyGone: false, status: 500 });
  });

  it('throws without calling Railway when no token is set', async () => {
    delete process.env.RAILWAY_API_TOKEN;
    const fetchImpl = vi.fn() as unknown as typeof fetch;
    await expect(readRailwayProjectName('proj-1', { fetchImpl })).rejects.toBeInstanceOf(RailwayApiError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
