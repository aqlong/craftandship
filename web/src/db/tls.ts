// Decides whether the Postgres pool asks the server for TLS. The policy is
// described in src/db/index.ts; this file holds the part that reads the URL.

import { isDevOrTestEnv } from '../middleware/auth';

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1']);

/**
 * The host node-postgres would connect to for a connection string, lower-cased
 * and without IPv6 brackets or percent-encoding. A Unix socket directory is
 * returned as its path ("/var/run/postgresql"). Returns '' when the string
 * names no host (the driver then uses PGHOST or its local default) and null
 * when it cannot be read as a Postgres URL at all.
 *
 * It follows the rules of the driver's own parser: a `host` query parameter
 * wins over the host in the authority, `postgresql://%2Fvar%2Frun%2Fpostgresql/db`
 * is a socket directory, and the password may contain an unencoded "@".
 */
export function databaseHost(databaseUrl: string): string | null {
  const text = databaseUrl.trim();
  if (!text) return null;
  // The driver treats a string that starts with "/" as a socket directory.
  if (text.startsWith('/')) return text.split(' ')[0];

  let parsed: URL;
  let emptyAuthorityHost = false;
  try {
    parsed = new URL(text);
  } catch {
    // "postgresql://user@/db" (user, empty host) is not a valid URL to the
    // WHATWG parser but is a valid Postgres URL: read it with a stand-in host.
    if (!text.includes('@/')) return null;
    try {
      parsed = new URL(text.replace('@/', '@placeholder.invalid/'));
      emptyAuthorityHost = true;
    } catch {
      return null;
    }
  }

  const scheme = parsed.protocol;
  if (scheme === 'socket:') return '';
  if (scheme !== 'postgres:' && scheme !== 'postgresql:') return null;

  const queryHost = parsed.searchParams.get('host');
  let host = queryHost || (emptyAuthorityHost ? '' : parsed.hostname);
  try {
    host = decodeURIComponent(host);
  } catch {
    return null;
  }
  host = host.toLowerCase();
  if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1);
  return host;
}

function isLocalHostName(host: string): boolean {
  return host.startsWith('/') || LOOPBACK_HOSTS.has(host);
}

/**
 * True when the database host is this machine: loopback or a Unix socket. A URL
 * that names no host connects to PGHOST when that is set, otherwise to the
 * local default.
 */
export function isLocalDatabase(databaseUrl: string): boolean {
  const host = databaseHost(databaseUrl);
  if (host === null) return false;
  if (host !== '') return isLocalHostName(host);
  const envHost = (process.env.PGHOST || '').trim().toLowerCase();
  return envHost === '' || isLocalHostName(envHost);
}

/** True for a host on Railway's private network (`*.railway.internal`). */
export function isRailwayInternalDatabase(databaseUrl: string): boolean {
  const host = databaseHost(databaseUrl);
  if (host === null) return false;
  return host === 'railway.internal' || host.endsWith('.railway.internal');
}

/**
 * The `ssl` option for the pool: false (no TLS) for a local or
 * Railway-internal database and for any host in a development or test
 * environment, otherwise TLS without certificate verification.
 *
 * "Development or test" is the app-wide definition (`isDevOrTestEnv`): NODE_ENV
 * is exactly "development" or "test". An unset NODE_ENV, "staging" and
 * "production" all get TLS to a remote database, so a deployment that forgot
 * NODE_ENV does not send its database traffic in the clear.
 */
export function poolSslOption(
  databaseUrl: string,
  nodeEnv: string | undefined,
): false | { rejectUnauthorized: false } {
  if (isRailwayInternalDatabase(databaseUrl) || isLocalDatabase(databaseUrl)) return false;
  return isDevOrTestEnv({ NODE_ENV: nodeEnv }) ? false : { rejectUnauthorized: false };
}
