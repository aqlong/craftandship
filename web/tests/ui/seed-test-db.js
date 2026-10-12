// seed-test-db.js
// Seeds the CI test database with schema + test user account.
// Run with: node tests/ui/seed-test-db.js
// Requires DATABASE_URL, DEV_TEST_EMAIL, DEV_TEST_PASSWORD env vars.

const { Pool } = require('pg');
const bcrypt = require('bcrypt');
const fs = require('fs');
const path = require('path');
const { seedUiFixtures } = require('./fixtures');

async function seed() {
  const pool = new Pool({ connectionString: process.env.DATABASE_URL });

  // Apply schema
  const schemaPath = path.join(__dirname, '../../src/db/schema.sql');
  const schema = fs.readFileSync(schemaPath, 'utf-8');
  await pool.query(schema);
  console.log('Schema applied');

  // Seed test user
  const email = process.env.DEV_TEST_EMAIL;
  const password = process.env.DEV_TEST_PASSWORD;
  if (!email || !password) {
    console.log('DEV_TEST_EMAIL or DEV_TEST_PASSWORD not set, skipping user seed');
    await pool.end();
    return;
  }

  const hash = await bcrypt.hash(password, 10);
  await pool.query(
    `INSERT INTO customers (id, email, password, name, email_verified)
     VALUES (gen_random_uuid(), $1, $2, $3, TRUE)
     ON CONFLICT (email) DO UPDATE SET email_verified = TRUE`,
    [email, hash, 'CI Test User']
  );
  console.log('Test user seeded:', email);

  // Fixture builds and a subscription for the browser tests (see fixtures.js):
  // a preview+approved build owned by the test keeper lets
  // tests/ui/lighthouse-a11y.spec.ts scan the lamp / make-public / published /
  // cancelled surfaces (resolveLampOwnership Path 1 = authed keeper owns the
  // build) and the price-slider spec drive make-public; a live build with an
  // active subscription backs the status-page, dashboard and
  // cancel-subscription specs. The preview build's fixed id must match
  // CAS_A11Y_LAMP_BUILD_ID in the UI test workflow env. Without it, the
  // build-route a11y scans skip, which is how two real contrast bugs shipped
  // unseen while the UI job was not running (2026-05-29).
  const fixtures = await seedUiFixtures(pool, email);
  console.log('Lamp a11y fixture build seeded:', fixtures.lampBuildId);
  console.log('Live fixture build seeded:', fixtures.liveBuildId);

  await pool.end();
}

seed().catch((err) => {
  console.error('Seed failed:', err);
  process.exit(1);
});
