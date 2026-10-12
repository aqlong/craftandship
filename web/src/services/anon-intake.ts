import crypto from 'crypto';
import pool from '../db';

// Anonymous intake preview: a visitor can start an intake chat without
// signing up. Draft builds are stored in the existing builds table with
// customer_id=NULL and a random session ID matching a browser cookie.
// On signup the draft is "claimed" by setting customer_id and nulling
// the session ID.

export interface AnonDraft {
  id: string;
  anonymous_session_id: string | null;
  customer_id: string | null;
  status: string;
  tier: string;
  created_at: Date;
}

/**
 * Generate a random 32-byte hex session ID suitable for the cas_anon_draft cookie.
 */
export function generateAnonSessionId(): string {
  return crypto.randomBytes(32).toString('hex');
}

/**
 * Create a new anonymous draft build tied to the given session ID.
 * Returns the new build's UUID. The caller is responsible for setting
 * the cookie on the response.
 */
export async function createAnonDraft(sessionId: string): Promise<{ buildId: string }> {
  if (!sessionId || typeof sessionId !== 'string' || sessionId.length < 16) {
    throw new Error('Invalid anonymous session ID');
  }

  const result = await pool.query<{ id: string }>(
    `INSERT INTO builds (customer_id, tier, status, anonymous_session_id)
     VALUES (NULL, 'build', 'intake', $1)
     RETURNING id`,
    [sessionId]
  );

  return { buildId: result.rows[0].id };
}

/**
 * Fetch an anonymous draft by build ID, verifying the caller owns it via
 * the session cookie. Returns null on mismatch, already-claimed, or missing.
 */
export async function getAnonDraftByBuildId(
  buildId: string,
  sessionId: string
): Promise<AnonDraft | null> {
  if (!buildId || !sessionId) return null;

  const result = await pool.query<AnonDraft>(
    `SELECT id, anonymous_session_id, customer_id, status, tier, created_at
     FROM builds
     WHERE id = $1
       AND customer_id IS NULL
       AND anonymous_session_id = $2`,
    [buildId, sessionId]
  );

  return result.rows[0] ?? null;
}

/**
 * Claim an anonymous draft for a newly-created customer. Transactional:
 * only updates rows that are still unclaimed AND match the session ID.
 * Returns true if exactly one row was updated, false otherwise (stale
 * cookie, already claimed, or wrong session).
 */
export async function claimAnonDraft(
  buildId: string,
  sessionId: string,
  customerId: string
): Promise<boolean> {
  if (!buildId || !sessionId || !customerId) return false;

  const result = await pool.query(
    `UPDATE builds
     SET customer_id = $1, anonymous_session_id = NULL
     WHERE id = $2
       AND customer_id IS NULL
       AND anonymous_session_id = $3
     RETURNING id`,
    [customerId, buildId, sessionId]
  );

  return (result.rowCount ?? 0) === 1;
}

/**
 * Claim the most recent anonymous draft tied to a session ID, without
 * requiring the caller to know the build ID. Used by POST /signup which
 * only has the cookie. Picks the newest unclaimed draft for the session
 * (there should only be one per cookie under normal flows).
 */
export async function claimAnonDraftBySessionId(
  sessionId: string,
  customerId: string
): Promise<{ claimed: boolean; buildId: string | null }> {
  if (!sessionId || !customerId) return { claimed: false, buildId: null };

  const result = await pool.query<{ id: string }>(
    `UPDATE builds
     SET customer_id = $1, anonymous_session_id = NULL
     WHERE id = (
       SELECT id FROM builds
       WHERE customer_id IS NULL
         AND anonymous_session_id = $2
       ORDER BY created_at DESC
       LIMIT 1
     )
     RETURNING id`,
    [customerId, sessionId]
  );

  const row = result.rows[0];
  return { claimed: !!row, buildId: row?.id ?? null };
}
