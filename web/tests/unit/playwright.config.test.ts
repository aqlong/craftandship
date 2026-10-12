// The Playwright suite starts the compiled server and then tests whatever
// answers on its port. These tests pin how that port is chosen and when an
// already-running server may be reused, because a suite that silently tests
// some other process on the port passes or fails for the wrong reason.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const TOUCHED_ENV = ['PORT', 'CI', 'CAS_UI_REUSE_SERVER'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of TOUCHED_ENV) savedEnv[key] = process.env[key];

type ServerConfig = {
  command: string;
  url: string;
  reuseExistingServer: boolean;
  env?: Record<string, string>;
};
type LoadedConfig = {
  use: { baseURL: string };
  webServer: ServerConfig;
};

async function loadConfig(env: Partial<Record<(typeof TOUCHED_ENV)[number], string>>): Promise<LoadedConfig> {
  for (const key of TOUCHED_ENV) delete process.env[key];
  for (const [key, value] of Object.entries(env)) process.env[key] = value;
  vi.resetModules();
  const mod = await import('../../playwright.config');
  return mod.default as unknown as LoadedConfig;
}

beforeEach(() => {
  for (const key of TOUCHED_ENV) delete process.env[key];
});

afterEach(() => {
  for (const key of TOUCHED_ENV) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

describe('playwright.config port', () => {
  it('defaults to 4000 for the base URL, the health check and the server it starts', async () => {
    const config = await loadConfig({});
    expect(config.use.baseURL).toBe('http://localhost:4000');
    expect(config.webServer.url).toBe('http://localhost:4000/health');
    expect(config.webServer.env?.PORT).toBe('4000');
  });

  it('follows PORT everywhere, so the suite and the server it starts cannot disagree', async () => {
    const config = await loadConfig({ PORT: '4412' });
    expect(config.use.baseURL).toBe('http://localhost:4412');
    expect(config.webServer.url).toBe('http://localhost:4412/health');
    expect(config.webServer.env?.PORT).toBe('4412');
  });

  it.each([['abc'], ['0'], ['70000'], ['40.5']])('refuses PORT=%s instead of guessing', async (bad) => {
    await expect(loadConfig({ PORT: bad })).rejects.toThrow(/PORT/);
  });
});

describe('playwright.config server reuse', () => {
  it('starts its own server by default, so an unrelated process on the port is an error', async () => {
    expect((await loadConfig({})).webServer.reuseExistingServer).toBe(false);
  });

  it('reuses a running server only when asked to, outside CI', async () => {
    expect((await loadConfig({ CAS_UI_REUSE_SERVER: '1' })).webServer.reuseExistingServer).toBe(true);
  });

  it('never reuses a running server in CI, even when asked to', async () => {
    expect((await loadConfig({ CI: 'true', CAS_UI_REUSE_SERVER: '1' })).webServer.reuseExistingServer).toBe(false);
    expect((await loadConfig({ CI: 'true' })).webServer.reuseExistingServer).toBe(false);
  });
});
