import fs from 'fs';
import path from 'path';
import pool from './index';

export async function runMigration(): Promise<void> {
  const schemaPath = path.join(__dirname, 'schema.sql');
  const schema = fs.readFileSync(schemaPath, 'utf-8');

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(schema);
    await client.query('COMMIT');
    console.log('Database migration completed successfully');
  } catch (err) {
    // Report the migration error, not a failure of the rollback that follows
    // it (the connection may already be gone).
    console.error('Database migration failed:', (err as Error).message);
    try {
      await client.query('ROLLBACK');
    } catch (rollbackErr) {
      console.error('Migration rollback failed:', (rollbackErr as Error).message);
    }
    throw err;
  } finally {
    client.release();
  }
}
