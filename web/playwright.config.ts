import { defineConfig, devices } from '@playwright/test';
import { uiOrigin, uiPort } from './tests/ui/port';

// Port: PORT (default 4000). The same value points the specs at the server and
// is handed to the server this config starts, so the two cannot disagree.
//
// Server: the config starts `node dist/server.js` itself (run `npm run build`
// first). It does not take over a server that is already answering on the
// port, because the suite would then test that process (an older build,
// another database, another NODE_ENV) and report on it as if it were this
// checkout. With a server already on the port, Playwright stops with "... is
// already used"; with another program on it that does not answer /health, the
// new server stops with "Port <n> is already in use" (shown below as
// [WebServer] output). Pick a free port (`PORT=4412 npm run test:ui`) or stop
// the other process.
//
// To run against a server you started yourself (for example `npm start` with
// the seeded test database and NODE_ENV=test), set CAS_UI_REUSE_SERVER=1. It
// has no effect when CI is set: a CI run never reuses a server.
const origin = uiOrigin();
const reuseRunningServer = !process.env.CI && ['1', 'true'].includes(process.env.CAS_UI_REUSE_SERVER ?? '');

export default defineConfig({
  testDir: './tests/ui',
  timeout: 30000,
  retries: 1,
  use: {
    baseURL: origin,
    headless: true,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: 'node dist/server.js',
    url: `${origin}/health`,
    env: { PORT: String(uiPort()) },
    // Show why the server did not start (for example a port already in use).
    stderr: 'pipe',
    reuseExistingServer: reuseRunningServer,
    timeout: 10000,
  },
});
