import pool from '../db';
import { sendAbandonNudgeEmail } from './email';

const BASE_URL = process.env.BASE_URL || 'https://craftandship.io';

/**
 * Sweep anonymous intake drafts (customer_id IS NULL, created via /try)
 * that are older than 7 days. Deletes the draft and its intake messages.
 * Separate from the authenticated abandon flow because anon drafts have
 * no email to nudge: they just expire silently.
 */
export async function cleanupStaleAnonDrafts(): Promise<number> {
  try {
    const stale = await pool.query<{ id: string }>(
      `SELECT id FROM builds
       WHERE customer_id IS NULL
         AND anonymous_session_id IS NOT NULL
         AND created_at < NOW() - INTERVAL '7 days'`
    );
    if (stale.rows.length === 0) return 0;

    const ids = stale.rows.map((r) => r.id);
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      await client.query('DELETE FROM intake_messages WHERE build_id = ANY($1)', [ids]);
      await client.query('DELETE FROM build_logs WHERE build_id = ANY($1::uuid[])', [ids]);
      const del = await client.query('DELETE FROM builds WHERE id = ANY($1)', [ids]);
      await client.query('COMMIT');
      const deleted = del.rowCount ?? 0;
      if (deleted > 0) {
        console.log(`[abandon-check] Swept ${deleted} stale anonymous draft(s)`);
      }
      return deleted;
    } catch (err) {
      await client.query('ROLLBACK').catch(() => {});
      console.error('[abandon-check] Anon sweep error:', (err as Error).message);
      return 0;
    } finally {
      client.release();
    }
  } catch (err) {
    console.error('[abandon-check] Anon sweep query error:', (err as Error).message);
    return 0;
  }
}

export async function checkAbandonedIntakes(): Promise<{ marked: number; deleted: number; anonDeleted: number }> {
  let marked = 0;
  let deleted = 0;
  let anonDeleted = 0;

  try {
    // Phase 1: Detect intakes with no messages for 24h
    const staleResult = await pool.query(
      `SELECT b.id, b.customer_id, c.email, c.name
       FROM builds b
       JOIN customers c ON c.id = b.customer_id
       LEFT JOIN (
         SELECT build_id, MAX(created_at) AS last_message_at
         FROM intake_messages
         GROUP BY build_id
       ) im ON im.build_id = b.id
       WHERE b.status = 'intake'
         AND COALESCE(im.last_message_at, b.created_at) < NOW() - INTERVAL '24 hours'`
    );

    for (const row of staleResult.rows) {
      const { id, email, name } = row as { id: string; email: string; name: string };
      // The status guard makes the transition atomic: if another instance (or
      // the customer, who may have just sent a message) got there first, this
      // UPDATE matches no row and no second nudge email goes out.
      const moved = await pool.query(
        `UPDATE builds SET status = 'abandoned', abandoned_at = NOW(), abandoned_email_sent_at = NOW()
          WHERE id = $1 AND status = 'intake'`,
        [id]
      );
      if ((moved.rowCount ?? 0) === 0) continue;
      marked++;

      const resumeUrl = `${BASE_URL}/intake/${id}`;
      sendAbandonNudgeEmail(email, name, resumeUrl).catch(err =>
        console.error('[abandon-check] Email error:', (err as Error).message)
      );
    }

    if (marked > 0) {
      console.log(`[abandon-check] Marked ${marked} build(s) as abandoned`);
    }

    // Phase 2: Clean up builds abandoned for 7+ days
    const oldResult = await pool.query(
      `SELECT id FROM builds WHERE status = 'abandoned' AND abandoned_at < NOW() - INTERVAL '7 days'`
    );

    if (oldResult.rows.length > 0) {
      const deleteIds = oldResult.rows.map((r: { id: string }) => r.id);
      const client = await pool.connect();
      try {
        await client.query('BEGIN');
        await client.query(
          `DELETE FROM feature_messages WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE build_id = ANY($1))`,
          [deleteIds]
        );
        await client.query('DELETE FROM feature_requests WHERE build_id = ANY($1)', [deleteIds]);
        await client.query('DELETE FROM intake_messages WHERE build_id = ANY($1)', [deleteIds]);
        await client.query('DELETE FROM build_logs WHERE build_id = ANY($1::uuid[])', [deleteIds]);
        await client.query('DELETE FROM dead_letter_builds WHERE build_id = ANY($1)', [deleteIds]);
        await client.query('UPDATE builds SET parent_build_id = NULL WHERE parent_build_id = ANY($1)', [deleteIds]);
        const delResult = await client.query('DELETE FROM builds WHERE id = ANY($1)', [deleteIds]);
        await client.query('COMMIT');
        deleted = delResult.rowCount ?? 0;
        console.log(`[abandon-check] Cleaned up ${deleted} abandoned build(s)`);
      } catch (err) {
        await client.query('ROLLBACK').catch(() => {});
        console.error('[abandon-check] Cleanup error:', (err as Error).message);
      } finally {
        client.release();
      }
    }
  } catch (err) {
    console.error('[abandon-check] Error:', (err as Error).message);
  }

  // Phase 3: sweep stale anonymous /try drafts (>7 days, never claimed)
  try {
    anonDeleted = await cleanupStaleAnonDrafts();
  } catch (err) {
    console.error('[abandon-check] Anon sweep error:', (err as Error).message);
  }

  return { marked, deleted, anonDeleted };
}

export function startAbandonCheck(intervalMs?: number): NodeJS.Timeout {
  const ms = intervalMs ?? parseInt(process.env.ABANDON_CHECK_INTERVAL_MS || '900000', 10);
  console.log(`[abandon-check] Started (interval: ${ms / 60000}m)`);
  setTimeout(() => checkAbandonedIntakes(), 5000);
  return setInterval(checkAbandonedIntakes, ms);
}

export async function resumeIfAbandoned(buildId: string): Promise<boolean> {
  const result = await pool.query('SELECT status FROM builds WHERE id = $1', [buildId]);
  if (result.rows.length === 0) return false;
  if ((result.rows[0] as { status: string }).status !== 'abandoned') return false;

  await pool.query(
    `UPDATE builds SET status = 'intake', abandoned_at = NULL, abandoned_email_sent_at = NULL WHERE id = $1`,
    [buildId]
  );
  console.log(`[abandon-check] Build ${buildId} resumed from abandoned`);
  return true;
}
