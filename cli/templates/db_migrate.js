const fs = require('fs');
const path = require('path');
const pool = require('./index');

async function migrate() {
  const client = await pool.connect();
  try {
    // Run schema
    const schema = fs.readFileSync(path.join(__dirname, 'schema.sql'), 'utf8');
    await client.query(schema);
    console.log('Schema migration complete.');

    // Seed only if users table is empty
    const { rows } = await client.query('SELECT COUNT(*) FROM users');
    if (parseInt(rows[0].count) === 0) {
      const seed = fs.readFileSync(path.join(__dirname, 'seed.sql'), 'utf8');
      // Split into individual statements and execute each independently.
      // One bad INSERT must not kill all seed data.
      //
      // Strip leading `-- comment` lines INSIDE each chunk before
      // checking for emptiness. The legacy filter `!s.startsWith('--')`
      // dropped any chunk that started with a comment, which is every
      // chunk whose preceding line was a comment, because the split on
      // `;\s*\n` attaches the trailing comment to the NEXT chunk. That
      // silently lost ~40% of MW-shape seed INSERTs (users table empty
      // on fresh DB, blocking magic-link login). See
      // tests/test_template_db_migrate.py for the regression guards.
      const statements = seed
        .split(/;\s*\n/)
        .map(s =>
          s.split('\n')
           .filter(l => !l.trim().startsWith('--'))
           .join('\n')
           .trim()
        )
        .filter(s => s.length > 0);
      let ok = 0;
      let failed = 0;
      for (const stmt of statements) {
        try {
          await client.query(stmt.endsWith(';') ? stmt : stmt + ';');
          ok++;
        } catch (err) {
          failed++;
          console.warn('Seed warning: ' + err.message.split('\n')[0]);
        }
      }
      console.log(`Seed data: ${ok} statements ok, ${failed} failed.`);
    } else {
      console.log('Seed data skipped (users table not empty).');
    }
  } catch (err) {
    console.error('Migration error:', err.message);
    throw err;
  } finally {
    client.release();
  }
}

// Export both forms: module.exports = migrate AND { migrate }
// Claude's server.js may use either require('./db/migrate') or { migrate } destructuring.
module.exports = migrate;
module.exports.migrate = migrate;
