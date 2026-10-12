import './instrument'; // Sentry init: must be first import
import * as Sentry from '@sentry/node';
import http from 'http';
import app from './app';
import pool from './db';
import { runMigration } from './db/migrate';
import { startAbandonCheck } from './services/abandon-check';
import { startEmailScheduler } from './services/email-scheduler';
import {
  startHarborCronSchedulers,
  type HarborCronIntervals,
} from './services/harbor-cron-scheduler';
import { getKnowledgeContentHash } from './services/support-knowledge';
import { warmSitemapLastmods } from './services/sitemap-lastmod';
import { describeListenError, listenOn } from './utils/listen';

const PORT = parseInt(process.env.PORT || '4000', 10);
const SHUTDOWN_TIMEOUT_MS = 30_000;

async function waitForDatabase(maxRetries: number = 15, baseDelayMs: number = 2000): Promise<void> {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      await runMigration();
      return;
    } catch (err) {
      if (attempt === maxRetries) {
        throw err;
      }
      const delay = Math.min(baseDelayMs * Math.pow(2, attempt - 1), 30000);
      console.log(
        `Database not ready (attempt ${attempt}/${maxRetries}), retrying in ${delay}ms: ${(err as Error).message}`
      );
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }
}

let server: http.Server;
let abandonCheckInterval: NodeJS.Timeout | undefined;
let emailSchedulerInterval: NodeJS.Timeout | undefined;
let harborCronIntervals: HarborCronIntervals = {};

async function gracefulShutdown(signal: string): Promise<void> {
  console.log(`${signal} received, shutting down gracefully...`);

  const forceTimer = setTimeout(() => {
    console.error('Forced shutdown after timeout');
    process.exit(1);
  }, SHUTDOWN_TIMEOUT_MS);
  forceTimer.unref();

  try {
    await new Promise<void>((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });
    if (abandonCheckInterval) {
      clearInterval(abandonCheckInterval);
    }
    if (emailSchedulerInterval) {
      clearInterval(emailSchedulerInterval);
    }
    if (harborCronIntervals.topicKb) {
      clearInterval(harborCronIntervals.topicKb);
    }
    if (harborCronIntervals.memoryLowUtility) {
      clearInterval(harborCronIntervals.memoryLowUtility);
    }
    if (harborCronIntervals.railFlag) {
      clearInterval(harborCronIntervals.railFlag);
    }
    if (harborCronIntervals.personaLearningDigest) {
      clearInterval(harborCronIntervals.personaLearningDigest);
    }
    console.log('HTTP server closed, draining database pool...');
    await pool.end();
    console.log('Database pool drained. Goodbye.');
    process.exit(0);
  } catch (err) {
    console.error('Error during shutdown:', (err as Error).message);
    process.exit(1);
  }
}

async function refreshSupportKbIfStale(): Promise<void> {
  const currentHash = getKnowledgeContentHash();
  try {
    const stored = await pool.query<{ value: string }>(
      "SELECT value FROM platform_metadata WHERE key = 'support_kb_knowledge_hash'"
    );
    const storedHash = stored.rows[0]?.value;
    if (storedHash === currentHash) {
      return;
    }
    const cleared = await pool.query('DELETE FROM support_kb');
    console.log(
      `[support_kb] Knowledge hash changed, clearing ${cleared.rowCount ?? 0} cached answers.`
    );
    await pool.query(
      `INSERT INTO platform_metadata (key, value, updated_at)
       VALUES ('support_kb_knowledge_hash', $1, NOW())
       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()`,
      [currentHash]
    );
  } catch (err) {
    console.error('[support_kb] Hash refresh failed:', (err as Error).message);
  }
}

// Last line of defense. Since Node 15 a promise rejection that nothing handles
// terminates the process, so one stray rejection in a fire-and-forget task or a
// background job would take the whole site down (and Railway stops restarting
// after restartPolicyMaxRetries). Request handlers are covered separately (app.ts
// forwards their rejections to the error middleware); this catches the rest.
// Log it, report it, and keep serving.
process.on('unhandledRejection', (reason: unknown) => {
  const err = reason instanceof Error ? reason : new Error(String(reason));
  console.error('Unhandled promise rejection:', err.message, err.stack);
  Sentry.captureException(err);
});

async function start(): Promise<void> {
  if (process.env.NODE_ENV === 'test') {
    console.warn(
      'WARNING: NODE_ENV=test. Request limiters and the trial-mint cap are raised or skipped, ' +
        'sessions are kept in memory, and background jobs do not start. The Haven cost caps ' +
        'are not relaxed. This mode exists for the test suite; do not serve real traffic with it.'
    );
  }
  try {
    await waitForDatabase();
    await refreshSupportKbIfStale();
  } catch (err) {
    console.error('Failed to start server after all retries:', (err as Error).message);
    process.exit(1);
  }

  // Bind the port before anything else starts: a second copy of the server, or
  // any other program on the port, must stop here with one clear line instead
  // of running the background jobs next to the copy that does serve.
  try {
    server = await listenOn(app, PORT);
  } catch (err) {
    console.error(describeListenError(err as NodeJS.ErrnoException, PORT));
    process.exit(1);
  }
  console.log(`Craft & Ship web server running on port ${PORT}`);

  // The sitemap's per-page dates come from git, read in the background with a
  // deadline (services/sitemap-lastmod.ts). Started here so they are usually
  // known before the first crawler asks; nothing waits for it.
  void warmSitemapLastmods();

  try {
    if (process.env.NODE_ENV !== 'test') {
      abandonCheckInterval = startAbandonCheck();
      emailSchedulerInterval = startEmailScheduler();
      harborCronIntervals = startHarborCronSchedulers();
    }
  } catch (err) {
    console.error('Failed to start background jobs:', (err as Error).message);
    process.exit(1);
  }

  process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
  process.on('SIGINT', () => gracefulShutdown('SIGINT'));
}

start();

export default app;
