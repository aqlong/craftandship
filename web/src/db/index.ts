import { Pool } from 'pg';
import dotenv from 'dotenv';
import { poolSslOption } from './tls';

dotenv.config();

// TLS to Postgres
// ---------------
//  - Railway private network (*.railway.internal), localhost (`localhost`,
//    `127.0.0.1`, `::1`, with or without credentials in the URL) and Unix
//    sockets: no TLS. None of them offers it, and the traffic does not leave
//    the host or the private network. (The localhost case also lets
//    NODE_ENV=production be run locally against a local database, for example
//    postgresql://localhost:5432/dbname.) The host is read from the parsed URL
//    (see tls.ts), not searched for in the string.
//  - Any other host unless NODE_ENV is exactly "development" or "test": TLS
//    WITHOUT certificate verification (`rejectUnauthorized: false`). That
//    includes "production", "staging" and an unset NODE_ENV, the same rule
//    isDevOrTestEnv applies everywhere else in the app. It is the setting the
//    production deployment ran with when it reached its database through
//    Railway's public proxy. It keeps a passive observer out; it does not
//    stop an active attacker who can sit between the app and the database
//    from impersonating the database.
//  - Any other host with NODE_ENV "development" or "test": no TLS (local
//    containers and CI databases rarely speak it).
//
// To get a verified connection, put the mode in the URL itself:
// `?sslmode=verify-full` (plus `&sslrootcert=/path/to/ca.pem` for a private
// CA). node-postgres parses the connection string after the options below, so
// an explicit sslmode there overrides the `ssl` value chosen here.
const databaseUrl = process.env.DATABASE_URL || '';
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: poolSslOption(databaseUrl, process.env.NODE_ENV),
  max: 10,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 15000,
});

pool.on('error', (err) => {
  console.error('Unexpected error on idle database client:', err.message);
});

export default pool;
