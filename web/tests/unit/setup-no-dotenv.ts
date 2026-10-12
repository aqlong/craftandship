// Unit tests must not depend on a developer's local web/.env.
//
// src/app.ts and src/db/index.ts call dotenv.config() at import time. The
// quickstart tells a reader to create web/.env (NODE_ENV=development and so
// on), and dotenv fills in every variable a test has deleted, so a test that
// means "NODE_ENV is unset" would silently get "development". Each test sets
// the environment it needs; this replaces dotenv with a no-op for the whole
// unit suite so the file on disk never takes part. CI has no web/.env in the
// unit job, so this makes a local run behave like CI.
import { vi } from 'vitest';

vi.mock('dotenv', () => {
  const config = () => ({ parsed: {} });
  return { default: { config }, config };
});
