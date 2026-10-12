// web/src/services/email-scheduler.ts
// Schedules and processes nurture email sequences.
// Uses the email_schedule table with send_at timestamps.
// A periodic check sends due emails and marks them sent.

import pool from '../db';
import {
  sendWelcomeHowItWorksEmail,
  sendWelcomeUseCasesEmail,
  sendWelcomeBetaUrgencyEmail,
  sendPostBuildCheckInEmail,
  sendPostBuildTestimonialEmail,
} from './email';

// Template name -> sender function mapping
type SenderFn = (to: string, name: string, ...args: string[]) => Promise<void>;

const TEMPLATE_SENDERS: Record<string, SenderFn> = {
  welcome_how_it_works: sendWelcomeHowItWorksEmail,
  welcome_use_cases: sendWelcomeUseCasesEmail,
  welcome_beta_urgency: sendWelcomeBetaUrgencyEmail,
  post_build_check_in: (to, name, appName, liveUrl) =>
    sendPostBuildCheckInEmail(to, name, appName, liveUrl),
  post_build_testimonial: (to, name, appName) =>
    sendPostBuildTestimonialEmail(to, name, appName),
};

/**
 * Schedule the welcome nurture sequence for a newly verified customer.
 * Called after email verification + welcome email.
 * Day 1: How it works, Day 3: Use cases, Day 7: Beta urgency
 */
export async function scheduleWelcomeSequence(customerId: string): Promise<void> {
  try {
    // Check if welcome sequence is already scheduled (prevent duplicates)
    const existing = await pool.query(
      `SELECT id FROM email_schedule
       WHERE customer_id = $1 AND template = 'welcome_how_it_works'
         AND cancelled_at IS NULL`,
      [customerId]
    );
    if (existing.rows.length > 0) return;

    await pool.query(
      `INSERT INTO email_schedule (customer_id, template, send_at)
       VALUES
         ($1, 'welcome_how_it_works', NOW() + INTERVAL '1 day'),
         ($1, 'welcome_use_cases', NOW() + INTERVAL '3 days'),
         ($1, 'welcome_beta_urgency', NOW() + INTERVAL '7 days')`,
      [customerId]
    );
    console.log(`[email-scheduler] Scheduled welcome sequence for customer ${customerId}`);
  } catch (err) {
    console.error('[email-scheduler] Failed to schedule welcome sequence:', (err as Error).message);
  }
}

/**
 * Schedule the post-build follow-up sequence when an app goes live.
 * Day 3: Check-in, Day 7: Testimonial request
 */
export async function schedulePostBuildSequence(
  customerId: string,
  appName: string,
  liveUrl: string
): Promise<void> {
  try {
    // Check for existing post-build sequence for this app (prevent duplicates)
    const existing = await pool.query(
      `SELECT id FROM email_schedule
       WHERE customer_id = $1 AND template = 'post_build_check_in'
         AND cancelled_at IS NULL
         AND context->>'app_name' = $2`,
      [customerId, appName]
    );
    if (existing.rows.length > 0) return;

    const context = JSON.stringify({ app_name: appName, live_url: liveUrl });
    await pool.query(
      `INSERT INTO email_schedule (customer_id, template, send_at, context)
       VALUES
         ($1, 'post_build_check_in', NOW() + INTERVAL '3 days', $2::jsonb),
         ($1, 'post_build_testimonial', NOW() + INTERVAL '7 days', $2::jsonb)`,
      [customerId, context]
    );
    console.log(`[email-scheduler] Scheduled post-build sequence for ${appName}`);
  } catch (err) {
    console.error('[email-scheduler] Failed to schedule post-build sequence:', (err as Error).message);
  }
}

/**
 * Cancel pending welcome sequence emails for a customer (e.g., when they start a build).
 * This prevents sending "have you started yet?" emails to someone who already built.
 */
export async function cancelWelcomeSequence(customerId: string): Promise<number> {
  try {
    const result = await pool.query(
      `UPDATE email_schedule SET cancelled_at = NOW()
       WHERE customer_id = $1
         AND template IN ('welcome_how_it_works', 'welcome_use_cases', 'welcome_beta_urgency')
         AND sent_at IS NULL AND cancelled_at IS NULL`,
      [customerId]
    );
    const count = result.rowCount ?? 0;
    if (count > 0) {
      console.log(`[email-scheduler] Cancelled ${count} welcome email(s) for customer ${customerId}`);
    }
    return count;
  } catch (err) {
    console.error('[email-scheduler] Failed to cancel welcome sequence:', (err as Error).message);
    return 0;
  }
}

/**
 * Process all due scheduled emails. Called periodically.
 *
 * Rows are CLAIMED atomically: one statement selects due rows with
 * FOR UPDATE SKIP LOCKED and stamps sent_at on them, so two web instances (a
 * rolling deploy, replicas) or two overlapping ticks can never send the same
 * email. The earlier version ran a bare SELECT ... FOR UPDATE through
 * pool.query(), outside a transaction, where the row locks were released as
 * soon as the statement returned and protected nothing.
 *
 * The trade-off is at-most-once delivery: if the process dies between the claim
 * and the send, that nurture email is lost rather than duplicated. A sender
 * that throws releases its claim so the next tick retries. The senders in
 * email.ts log failures and resolve instead of throwing, so a failed Resend
 * call is recorded in email_log as 'failed' but is not retried from here.
 */
export async function processDueEmails(): Promise<number> {
  let processed = 0;
  try {
    const result = await pool.query(
      `WITH due AS (
         SELECT id FROM email_schedule
          WHERE send_at <= NOW()
            AND sent_at IS NULL
            AND cancelled_at IS NULL
          ORDER BY send_at ASC
          LIMIT 50
          FOR UPDATE SKIP LOCKED
       ), claimed AS (
         UPDATE email_schedule es
            SET sent_at = NOW()
           FROM due
          WHERE es.id = due.id
          RETURNING es.id, es.template, es.context, es.customer_id, es.send_at
       )
       SELECT claimed.id, claimed.template, claimed.context, c.email, c.name
         FROM claimed
         JOIN customers c ON c.id = claimed.customer_id
        ORDER BY claimed.send_at ASC`
    );

    for (const row of result.rows) {
      const { id, template, context, email, name } = row as {
        id: string;
        template: string;
        context: Record<string, string>;
        email: string;
        name: string;
      };

      const sender = TEMPLATE_SENDERS[template];
      if (!sender) {
        console.warn(`[email-scheduler] Unknown template: ${template}, cancelling`);
        await pool.query(
          'UPDATE email_schedule SET sent_at = NULL, cancelled_at = NOW() WHERE id = $1',
          [id]
        );
        continue;
      }

      try {
        // Build args based on template type
        if (template.startsWith('post_build_')) {
          await sender(email, name, context.app_name || '', context.live_url || '');
        } else {
          await sender(email, name);
        }
        processed++;
      } catch (err) {
        console.error(`[email-scheduler] Failed to send ${template} to ${email}:`, (err as Error).message);
        // Release the claim so the next tick retries this row.
        await pool
          .query('UPDATE email_schedule SET sent_at = NULL WHERE id = $1', [id])
          .catch((releaseErr: Error) =>
            console.error('[email-scheduler] Failed to release claim:', releaseErr.message)
          );
      }
    }

    if (processed > 0) {
      console.log(`[email-scheduler] Processed ${processed} scheduled email(s)`);
    }
  } catch (err) {
    console.error('[email-scheduler] processDueEmails error:', (err as Error).message);
  }
  return processed;
}

/**
 * Start the periodic email scheduler check.
 * Default interval: 5 minutes.
 */
export function startEmailScheduler(intervalMs?: number): NodeJS.Timeout {
  const ms = intervalMs ?? parseInt(process.env.EMAIL_SCHEDULER_INTERVAL_MS || '300000', 10);
  console.log(`[email-scheduler] Started (interval: ${ms / 60000}m)`);
  // First check after 10 seconds (let server stabilize)
  setTimeout(() => processDueEmails(), 10_000);
  return setInterval(processDueEmails, ms);
}
