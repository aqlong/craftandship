import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { Client } from 'pg';
import { scanSql } from './schema-sql-scan';

// src/db/migrate.ts applies the whole of schema.sql, in one transaction, on
// every boot. These tests pin the properties that make that safe:
//
//   1. Static: no statement outside a guarded DO block mutates or removes
//      data, and every CREATE / ADD COLUMN is IF NOT EXISTS.
//   2. Every statement that changes rows, wherever it stands, is one of the
//      nine listed below, carries a reason of its own, and sits behind the
//      guard written for it (a block that merely contains some IF EXISTS does
//      not count). No statement names the status `live`.
//   3. The reader these tests use (./schema-sql-scan.ts) and the Python reader
//      the pre-commit hook uses (scripts/check_destructive_ddl.py) are held to
//      the same corpus of awkward inputs (./fixtures/sql-scan-corpus.json).
//   4. Optional integration: when CAS_SCHEMA_TEST_DATABASE_URL points at a
//      THROWAWAY empty database, apply the file twice and prove the data
//      backfills do not re-run. Skipped by default (the unit suite needs no
//      database).

const schemaPath = path.join(__dirname, '../../src/db/schema.sql');
const schema = fs.readFileSync(schemaPath, 'utf-8');

// The file cut into statements the way PostgreSQL cuts it: a dollar quote with
// or without a tag, a string, a comment and a quoted identifier hide a
// semicolon, and a DO block is one statement.
const scan = scanSql(schema);
const statements = scan.statements;
const topLevel = statements.filter((s) => !s.insideDoBlock);
const doBlocks = statements.filter((s) => s.insideDoBlock);

describe('schema.sql is safe to apply on every boot (static)', () => {
  it('parses into a plausible number of statements', () => {
    expect(statements.length).toBeGreaterThan(150);
    expect(doBlocks.length).toBeGreaterThanOrEqual(6);
  });

  it('cuts the file where its statements end, as a plain line-by-line count does', () => {
    // A third way of counting, for the real file only: a line that ends in a
    // semicolon outside a $$ body ends a statement (the file uses no tagged
    // quote at the moment, which is why this is not a reader of its own).
    // tests/test_destructive_ddl_corpus.py makes the same count for the
    // Python reader, so both readers are held to one number.
    let inBody = false;
    let ends = 0;
    for (const line of schema.split('\n')) {
      const code = line.split('--')[0];
      if ((code.match(/\$\$/g) ?? []).length % 2 === 1) inBody = !inBody;
      if (!inBody && code.trimEnd().endsWith(';')) ends += 1;
    }
    expect(statements.length).toBe(ends);
  });

  it('has no top-level statement that changes or removes rows', () => {
    const offenders = topLevel
      .filter((s) => /^(UPDATE|DELETE|INSERT|TRUNCATE)\b/i.test(s.text))
      .map((s) => s.text.slice(0, 80));
    expect(offenders).toEqual([]);
    // Nor one that starts with something else (WITH ... UPDATE, MERGE, COPY ... FROM, PREPARE ... AS UPDATE):
    // the reader finds a row-changing command wherever it stands, acknowledged or not.
    expect(scan.all.filter((f) => f.depth === 0)).toEqual([]);
  });

  it('has no top-level DROP of a table, column or constraint', () => {
    const offenders = topLevel
      .filter(
        (s) =>
          /^DROP\s+TABLE\b/i.test(s.text) ||
          /\bDROP\s+COLUMN\b/i.test(s.text) ||
          /\bDROP\s+CONSTRAINT\b/i.test(s.text)
      )
      .map((s) => s.text.slice(0, 80));
    expect(offenders).toEqual([]);
  });

  it('creates every table and index with IF NOT EXISTS', () => {
    const offenders = topLevel
      .filter(
        (s) =>
          /^CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\b/i.test(s.text) &&
          !/^CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\b/i.test(s.text)
      )
      .map((s) => s.text.slice(0, 80));
    expect(offenders).toEqual([]);
  });

  it('adds every column with ADD COLUMN IF NOT EXISTS', () => {
    const offenders = topLevel
      .filter((s) => /\bADD\s+COLUMN\b/i.test(s.text) && !/\bADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\b/i.test(s.text))
      .map((s) => s.text.slice(0, 80));
    expect(offenders).toEqual([]);
  });

  it('reads the file whole: nothing unclosed, and a plausible number of statements', () => {
    expect(scan.unreadable).toBeNull();
    expect(statements.length).toBeGreaterThan(150);
  });

  it('changes rows only in the nine statements listed here, each in a DO block behind the guard written for it, each with a reason of its own', () => {
    // Adding a backfill to schema.sql means adding it to this list on purpose.
    // A block that merely contains some IF EXISTS (about any table) is not a
    // guard for a row change in it, and a reason written above the block does
    // not speak for a statement added to the block later: the reader counts a
    // reason only on the statement's own line or directly above it.
    const inventory: Array<{ change: string; guard: RegExp }> = [
      {
        change: 'UPDATE customers',
        guard: /IF NOT EXISTS \([\s\S]*?table_name = 'customers'[\s\S]*?column_name = 'email_verified'\s*\) THEN\s+ALTER TABLE customers ADD COLUMN email_verified[^;]*;\s+UPDATE customers SET email_verified = TRUE;/,
      },
      {
        change: 'UPDATE password_reset_tokens',
        guard: /IF NOT EXISTS \([\s\S]*?table_name = 'password_reset_tokens' AND column_name = 'token_hashed'\s*\) THEN\s+ALTER TABLE password_reset_tokens ADD COLUMN token_hashed[^;]*;\s+UPDATE password_reset_tokens SET used_at = NOW\(\) WHERE used_at IS NULL;/,
      },
      {
        change: 'UPDATE email_verification_tokens',
        guard: /column_name = 'token_hashed'\s*\) THEN[\s\S]*ALTER TABLE email_verification_tokens ADD COLUMN token_hashed[^;]*;\s+UPDATE email_verification_tokens SET used_at = NOW\(\) WHERE used_at IS NULL;/,
      },
      {
        change: 'UPDATE agent_tasks',
        guard: /IF NOT EXISTS \([\s\S]*?pg_indexes[\s\S]*?indexname = 'idx_agent_tasks_dedup'\s*\) THEN\s+UPDATE agent_tasks SET status = 'expired'/,
      },
      {
        change: 'UPDATE builds',
        guard: /IF EXISTS \(SELECT 1 FROM builds WHERE qa_results IS NULL\) THEN\s+UPDATE builds SET qa_results = '\[\]'::jsonb WHERE qa_results IS NULL;/,
      },
      {
        change: 'UPDATE builds',
        guard: /IF NOT EXISTS \([\s\S]*?table_name = 'builds'[\s\S]*?column_name = 'build_engine'\s*\) THEN\s+ALTER TABLE builds ADD COLUMN build_engine[^;]*;\s+UPDATE builds\s+SET build_engine = CASE/,
      },
      {
        change: 'UPDATE builds',
        guard: /IF EXISTS \(\s*SELECT 1 FROM builds b\s+WHERE NOT b\.is_harbor[\s\S]*?\) THEN\s+WITH RECURSIVE harbor AS[\s\S]*?\)\s+UPDATE builds b SET is_harbor = TRUE FROM harbor h WHERE b\.id = h\.id AND NOT b\.is_harbor;/,
      },
      {
        change: 'UPDATE trial_keepers',
        guard: /IF EXISTS \(SELECT 1 FROM trial_keepers WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL\) THEN\s+UPDATE trial_keepers SET handoff_at = NOW\(\) WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL;/,
      },
      {
        change: 'UPDATE keepers',
        guard: /IF EXISTS \(SELECT 1 FROM keepers WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL\) THEN\s+UPDATE keepers SET handoff_at = NOW\(\) WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL;/,
      },
    ];
    expect(scan.all.map((f) => `${f.kind} ${f.table}`)).toEqual(inventory.map((i) => i.change));
    scan.all.forEach((found, position) => {
      const block = statements[found.statementIndex];
      expect(found.depth, `${found.kind} ${found.table}`).toBe(1);
      expect(found.container, `${found.kind} ${found.table}`).toBe('do');
      expect(block.insideDoBlock).toBe(true);
      expect(found.acknowledgedBy, `${found.kind} ${found.table} has no reason of its own`).toMatch(/--\s*SAFE:\s*\S.{20,}/);
      expect(block.text, `${found.kind} ${found.table} is not behind its guard`).toMatch(inventory[position].guard);
    });
    // and nothing is left unacknowledged
    expect(scan.findings).toEqual([]);
  });

  it('writes no statement that names the status live, acknowledged or not', () => {
    // Three statements of the web application write `live` and nothing else
    // does (docs/THREAT_MODEL.md, "Review gate and publication"). schema.sql
    // runs at every boot; a row change in it that names the status, or a
    // default of live, is refused whatever comment is above it.
    expect(scan.all.filter((f) => f.kind === 'LIVE')).toEqual([]);
  });

  it('runs the email_verified backfill only when the column is first added', () => {
    // Regression: an unguarded `UPDATE customers SET email_verified = TRUE
    // WHERE ... created_at < NOW() - INTERVAL '1 minute'` ran on every boot
    // and verified every unverified signup older than a minute on each deploy.
    const backfill = statements.filter((s) =>
      /UPDATE\s+customers\s+SET\s+email_verified/i.test(s.text)
    );
    expect(backfill).toHaveLength(1);
    expect(backfill[0].insideDoBlock).toBe(true);
    expect(backfill[0].text).toMatch(/IF\s+NOT\s+EXISTS[\s\S]*information_schema\.columns[\s\S]*column_name\s*=\s*'email_verified'/i);
    // The UPDATE must sit after the ADD COLUMN inside the guarded branch.
    const addAt = backfill[0].text.search(/ADD\s+COLUMN\s+email_verified/i);
    const updAt = backfill[0].text.search(/UPDATE\s+customers/i);
    expect(addAt).toBeGreaterThan(-1);
    expect(updAt).toBeGreaterThan(addAt);
  });

  it('runs the build_engine backfill only when the column is first added', () => {
    const backfill = statements.filter((s) => /UPDATE\s+builds\s+SET\s+build_engine/i.test(s.text));
    expect(backfill).toHaveLength(1);
    expect(backfill[0].insideDoBlock).toBe(true);
    expect(backfill[0].text).toMatch(/column_name\s*=\s*'build_engine'/i);
  });

  it('does not expire agent_tasks rows with a NULL context_hash', () => {
    // DISTINCT ON treats NULLs as equal, but the unique index treats them as
    // distinct, so NULL-hash tasks are never duplicates of each other.
    const dedupe = statements.find((s) => /UPDATE\s+agent_tasks\s+SET\s+status\s*=\s*'expired'/i.test(s.text));
    expect(dedupe).toBeDefined();
    expect(dedupe!.insideDoBlock).toBe(true);
    expect(dedupe!.text).toMatch(/context_hash\s+IS\s+NOT\s+NULL/i);
    expect(dedupe!.text).toMatch(/pg_indexes[\s\S]*idx_agent_tasks_dedup/i);
  });

  it('records Harbor origin on the build row: a column, a trigger that copies it from the parent, and a guarded backfill', () => {
    expect(schema).toMatch(/ALTER TABLE builds ADD COLUMN IF NOT EXISTS is_harbor BOOLEAN NOT NULL DEFAULT FALSE;/);
    // Every insert that names a parent inherits the flag, whatever component sends it.
    const trigger = statements.find((s) => /^CREATE TRIGGER builds_inherit_is_harbor\b/i.test(s.text));
    expect(trigger).toBeDefined();
    expect(trigger!.text).toMatch(/BEFORE INSERT OR UPDATE OF parent_build_id ON builds\s+FOR EACH ROW/i);
    const fn = statements.find((s) => /^CREATE OR REPLACE FUNCTION builds_inherit_is_harbor\b/i.test(s.text));
    expect(fn).toBeDefined();
    // It only ever sets the flag: the body assigns is_harbor from the parent
    // when the row is not already marked, and has no branch that clears it.
    expect(fn!.text).toMatch(/IF NEW\.parent_build_id IS NOT NULL AND NOT NEW\.is_harbor THEN/);
    expect(fn!.text).not.toMatch(/is_harbor\s*:=\s*FALSE/i);

    // The backfill reads the stamps and the lineage, and runs only when a row is missing the flag.
    const backfill = statements.filter((s) => /UPDATE\s+builds\s+b\s+SET\s+is_harbor\s*=\s*TRUE/i.test(s.text));
    expect(backfill).toHaveLength(1);
    expect(backfill[0].insideDoBlock).toBe(true);
    expect(backfill[0].text).toMatch(/IF EXISTS \(\s*SELECT 1 FROM builds b\s+WHERE NOT b\.is_harbor/i);
    expect(backfill[0].text).toMatch(/trial_keepers t WHERE t\.handoff_build_id = b\.id/);
    expect(backfill[0].text).toMatch(/keepers k WHERE k\.handoff_build_id = b\.id/);
    expect(backfill[0].text).toMatch(/WITH RECURSIVE harbor AS/i);
    // And it sits after the columns it reads have been added.
    expect(schema.indexOf('ADD COLUMN IF NOT EXISTS is_harbor')).toBeGreaterThan(
      schema.indexOf('ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS handoff_build_id')
    );
  });

  it('lets keepers.subscription_status hold every Stripe status, for new and for existing databases', () => {
    const all = "'active', 'trialing', 'past_due', 'unpaid', 'paused',\\s+'incomplete', 'incomplete_expired', 'canceled'";
    // New database: the column is created with the full list.
    expect(schema).toMatch(new RegExp(`ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'active'\\s+CHECK \\(subscription_status IN \\(${all}\\)\\)`));
    // Existing database: widened once, guarded on the definition already containing 'unpaid'.
    const widen = statements.find((s) => s.text.includes('DROP CONSTRAINT IF EXISTS keepers_subscription_status_check'));
    expect(widen).toBeDefined();
    expect(widen!.text).toMatch(/pg_get_constraintdef\(oid\) LIKE '%''unpaid''%'/);
    expect(widen!.text).toMatch(new RegExp(`CHECK \\(subscription_status IN \\(${all}\\)\\)`));
  });

  it('lets build_start_events.kind hold a dispatch, for new and for existing databases', () => {
    const all = "'retry', 'rebuild', 'revision', 'dispatch'";
    // New database: the table is created with the four kinds.
    expect(schema).toMatch(new RegExp(`kind VARCHAR\\(20\\) NOT NULL CHECK \\(kind IN \\(${all}\\)\\)`));
    // Existing database: widened once, guarded on the definition already containing 'dispatch'.
    const widen = statements.find((s) => s.text.includes('DROP CONSTRAINT IF EXISTS build_start_events_kind_check'));
    expect(widen).toBeDefined();
    expect(widen!.text).toMatch(/pg_get_constraintdef\(oid\) LIKE '%''dispatch''%'/);
    expect(widen!.text).toMatch(new RegExp(`CHECK \\(kind IN \\(${all}\\)\\)`));
  });

  it('rewrites constraints only through guarded DO blocks', () => {
    for (const name of [
      'builds_status_check',
      'builds_review_status_check',
      'beta_feedback_customer_id_fkey',
      'keepers_subscription_status_check',
      'build_start_events_kind_check',
    ]) {
      const stmts = statements.filter((s) => s.text.includes(`DROP CONSTRAINT`) && s.text.includes(name));
      expect(stmts.length, name).toBeGreaterThan(0);
      for (const s of stmts) {
        expect(s.insideDoBlock, name).toBe(true);
        expect(s.text, name).toMatch(/pg_constraint/i);
      }
    }
  });

  it('indexes the columns the hot queries filter by', () => {
    expect(schema).toMatch(/idx_intake_messages_build_id\s+ON\s+intake_messages\s*\(build_id,\s*created_at\)/i);
    expect(schema).toMatch(/idx_builds_customer_id\s+ON\s+builds\s*\(customer_id\)/i);
    expect(schema).toMatch(/idx_builds_parent_build_id\s+ON\s+builds\s*\(parent_build_id\)/i);
    expect(schema).toMatch(/idx_build_logs_build_id\s+ON\s+build_logs\s*\(build_id,\s*id\)/i);
  });
});

interface CorpusCase {
  group: string;
  name: string;
  sql: string;
  statements?: number;
  findings?: Array<[number, string]>;
  unreadable?: boolean;
  splice?: boolean;
}

const corpus: CorpusCase[] = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures/sql-scan-corpus.json'), 'utf-8')
).cases;

describe('the reader of schema.sql agrees with scripts/check_destructive_ddl.py on a shared corpus', () => {
  // tests/test_destructive_ddl_corpus.py reads the same file and holds the
  // Python reader to the same expectations, case by case.
  it('has the cases the Python test counts on', () => {
    expect(corpus.length).toBeGreaterThanOrEqual(150);
    expect(new Set(corpus.map((c) => c.sql)).size).toBe(corpus.length);
    expect(new Set(corpus.map((c) => c.group))).toEqual(new Set(['split', 'rows', 'bodies', 'acknowledged', 'live']));
    expect(corpus.filter((c) => c.splice).length).toBeGreaterThanOrEqual(40);
  });

  it.each(corpus.map((c) => [`${c.group}: ${c.name}`, c] as const))('%s', (_name, entry) => {
    const result = scanSql(entry.sql);
    if (entry.unreadable) {
      expect(result.unreadable, 'an unclosed quote or comment is refused, not skipped').not.toBeNull();
      return;
    }
    expect(result.unreadable).toBeNull();
    expect(result.statements.length).toBe(entry.statements);
    expect(result.findings.map((f) => [f.line, f.kind])).toEqual(entry.findings);
  });
});

describe('a row change added to schema.sql is refused wherever it is put', () => {
  const schemaLines = schema.split('\n').length - (schema.endsWith('\n') ? 1 : 0);
  const endOfFirstDoBlock = schema.indexOf('\nEND $$;\n') + '\nEND $$;\n'.length;
  const linesBeforeThat = schema.slice(0, endOfFirstDoBlock).split('\n').length - 1;
  const spliced = corpus.filter((c) => c.splice);

  it('passes as it stands', () => {
    expect(scan.findings).toEqual([]);
    expect(scan.unreadable).toBeNull();
  });

  it.each(spliced.map((c) => [`${c.group}: ${c.name}`, c] as const))('at the end: %s', (_name, entry) => {
    const result = scanSql(`${schema}${schema.endsWith('\n') ? '' : '\n'}${entry.sql}\n`);
    expect(result.unreadable).toBeNull();
    expect(result.findings.length, 'refused').toBeGreaterThan(0);
    expect(result.findings.every((f) => f.line > schemaLines), 'and the finding points into the added text').toBe(true);
  });

  it.each(spliced.map((c) => [`${c.group}: ${c.name}`, c] as const))('after the first DO block: %s', (_name, entry) => {
    const result = scanSql(`${schema.slice(0, endOfFirstDoBlock)}${entry.sql}\n${schema.slice(endOfFirstDoBlock)}`);
    expect(result.unreadable).toBeNull();
    expect(result.findings.length, 'refused').toBeGreaterThan(0);
    const added = entry.sql.split('\n').length;
    expect(result.findings.every((f) => f.line > linesBeforeThat && f.line <= linesBeforeThat + added), 'and nothing real is blamed for it').toBe(true);
  });

  it('refuses the reviewer\'s mutation, a tagged DO block that sets a build live, at the end of the real file', () => {
    const mutation = `\nDO $m$\nBEGIN\n  UPDATE builds SET status = 'live' WHERE id IS NOT NULL;\nEND\n$m$;\n`;
    const result = scanSql(schema + mutation);
    // (The reader this file used to have toggled on $$ alone, so it saw a tagged block as opaque text and passed this.)
    expect(result.findings.map((f) => f.kind).sort()).toEqual(['LIVE', 'UPDATE']);
  });
});

const integrationUrl = process.env.CAS_SCHEMA_TEST_DATABASE_URL;

describe.skipIf(!integrationUrl)('schema.sql against a throwaway Postgres (integration)', () => {
  it('applies cleanly twice and does not re-run the email_verified backfill', async () => {
    const client = new Client({ connectionString: integrationUrl });
    await client.connect();
    try {
      // Same shape as runMigration(): BEGIN, the whole file as one query, COMMIT.
      await client.query('BEGIN');
      await client.query(schema);
      await client.query('COMMIT');

      await client.query(
        `INSERT INTO customers (email, password, name, created_at)
         VALUES ('schema-probe@example.test', 'x', 'Probe', NOW() - INTERVAL '2 hours')`
      );

      await client.query('BEGIN');
      await client.query(schema);
      await client.query('COMMIT');

      const row = await client.query(
        `SELECT email_verified FROM customers WHERE email = 'schema-probe@example.test'`
      );
      expect(row.rows[0].email_verified).toBe(false);
    } finally {
      await client.query('ROLLBACK').catch(() => undefined);
      await client.end();
    }
  });

  it('a database whose build_start_events CHECK predates the dispatch kind is widened once, keeps its rows, and is then left alone', async () => {
    const client = new Client({ connectionString: integrationUrl });
    await client.connect();
    const apply = async (): Promise<void> => {
      await client.query('BEGIN');
      await client.query(schema);
      await client.query('COMMIT');
    };
    const constraint = async (): Promise<{ oid: number; def: string }> =>
      (
        await client.query(
          `SELECT oid::int AS oid, pg_get_constraintdef(oid) AS def FROM pg_constraint
           WHERE conrelid = 'build_start_events'::regclass AND conname = 'build_start_events_kind_check'`
        )
      ).rows[0];
    try {
      await apply();
      // Put the table back the way an older database has it: three kinds.
      // (NOT VALID, because another test file sharing this database may
      // already have written dispatch rows; the old rule is what matters.)
      await client.query('ALTER TABLE build_start_events DROP CONSTRAINT build_start_events_kind_check');
      await client.query(
        `ALTER TABLE build_start_events ADD CONSTRAINT build_start_events_kind_check
         CHECK (kind IN ('retry', 'rebuild', 'revision')) NOT VALID`
      );
      const owner = await client.query(
        `INSERT INTO customers (email, password, name) VALUES ('schema-ledger-probe-' || gen_random_uuid() || '@example.test', 'x', 'Probe') RETURNING id`
      );
      const customerId = owner.rows[0].id as string;
      const insert = (kind: string) =>
        client.query('INSERT INTO build_start_events (customer_id, build_id, kind) VALUES ($1, gen_random_uuid(), $2)', [customerId, kind]);
      await insert('retry');
      await expect(insert('dispatch')).rejects.toThrow(/build_start_events_kind_check/);

      // The next boot.
      await apply();
      const widened = await constraint();
      expect(widened.def).toContain("'dispatch'");
      await insert('dispatch');
      const kinds = await client.query('SELECT kind FROM build_start_events WHERE customer_id = $1 ORDER BY id', [customerId]);
      expect(kinds.rows.map((r) => r.kind)).toEqual(['retry', 'dispatch']);
      await expect(insert('not-a-kind')).rejects.toThrow(/build_start_events_kind_check/);

      // And the boot after that does not drop and re-add it again.
      await apply();
      expect((await constraint()).oid).toBe(widened.oid);
    } finally {
      await client.query('ROLLBACK').catch(() => undefined);
      await client.end();
    }
  });
});
