// Fixture rows for the browser tests, shared by seed-test-db.js (CI seeds once
// before the run) and the specs (which re-apply them, so a spec also runs
// against a database that was seeded earlier). Every statement is an idempotent
// upsert keyed on a fixed id. All values are synthetic.

// Preview + approved: the make-public page (price slider) and the a11y sweep.
const LAMP_BUILD_ID = process.env.CAS_A11Y_LAMP_BUILD_ID || 'a11c0de0-0000-4000-8000-00000000a11c';
// Live, with a screenshot and an active subscription: the status page, the
// dashboard rebuild dialog and the cancel-subscription confirmation.
const LIVE_BUILD_ID = 'a11c0de0-0000-4000-8000-00000000b001';

// A 1x1 PNG, so the status page has a screenshot cell to open.
const PIXEL_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';

async function seedUiFixtures(pool, email) {
  const found = await pool.query('SELECT id FROM customers WHERE email = $1', [email]);
  const customerId = found.rows[0] && found.rows[0].id;
  if (!customerId) throw new Error('seedUiFixtures: no customer with that email; seed the user first');

  await pool.query(
    `INSERT INTO builds (id, customer_id, tier, status, review_status, app_name,
                         preview_url, preview_approved_at, created_at, updated_at)
     VALUES ($1, $2, 'build', 'preview', 'approved', 'Aria Test Studio',
             'http://localhost:4600', NOW(), NOW(), NOW())
     ON CONFLICT (id) DO UPDATE
       SET status = 'preview', review_status = 'approved',
           customer_id = EXCLUDED.customer_id, preview_url = EXCLUDED.preview_url`,
    [LAMP_BUILD_ID, customerId]
  );

  await pool.query(
    `INSERT INTO builds (id, customer_id, tier, status, review_status, app_name, live_url,
                         screenshots, reviewed_at, completed_at, created_at, updated_at)
     VALUES ($1, $2, 'build', 'live', 'approved', 'Aria Live Studio', 'https://studio.example.test',
             $3::jsonb, NOW(), NOW(), NOW(), NOW())
     ON CONFLICT (id) DO UPDATE
       SET status = 'live', review_status = 'approved', customer_id = EXCLUDED.customer_id,
           live_url = EXCLUDED.live_url, screenshots = EXCLUDED.screenshots`,
    [LIVE_BUILD_ID, customerId, JSON.stringify([{ page: 'Home', phase: 'deploy', data: PIXEL_PNG_BASE64 }])]
  );

  // The keeper row carries the subscription the lamp page reads; the form to
  // cancel it only appears while it is active and not already ending.
  await pool.query(
    `INSERT INTO keepers (customer_id, companion_name, companion_assigned)
     VALUES ($1, 'Haven', 'Haven')
     ON CONFLICT (customer_id) DO NOTHING`,
    [customerId]
  );
  await pool.query(
    `UPDATE keepers
        SET subscription_status = 'active', stripe_subscription_id = 'sub_ui_fixture',
            sliding_scale_amount_cents = 3900
      WHERE customer_id = $1`,
    [customerId]
  );
  await pool.query(
    `DELETE FROM harbor_subscriptions WHERE stripe_subscription_id = 'sub_ui_fixture'`
  );

  return { customerId, lampBuildId: LAMP_BUILD_ID, liveBuildId: LIVE_BUILD_ID };
}

module.exports = { seedUiFixtures, LAMP_BUILD_ID, LIVE_BUILD_ID };
