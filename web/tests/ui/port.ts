// The port the browser suite talks to. It is the server's own PORT variable
// (src/server.ts), so `PORT=4412 npm run test:ui` starts the server on 4412 and
// points every spec at it. 4000 when PORT is unset.

const DEFAULT_PORT = 4000;

/** The suite's port. Throws on a PORT that is not a whole number from 1 to 65535. */
export function uiPort(env: NodeJS.ProcessEnv = process.env): number {
  const raw = (env.PORT ?? '').trim();
  if (raw === '') return DEFAULT_PORT;
  const port = Number(raw);
  if (!/^\d+$/.test(raw) || port < 1 || port > 65535) {
    throw new Error(`PORT must be a whole number from 1 to 65535 for the browser suite, got "${raw}".`);
  }
  return port;
}

/** http://localhost:<port>, the origin every spec browses. */
export function uiOrigin(env: NodeJS.ProcessEnv = process.env): string {
  return `http://localhost:${uiPort(env)}`;
}
