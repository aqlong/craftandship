/**
 * Make-it-public blessing pool.
 *
 * When a keeper makes their site public, the runtime appends a small
 * companion turn to /lighthouse/table containing one of these blessings.
 * The keeper sees it the next time they open the table; matches the
 * "letter, not chat" pacing of /lighthouse/note.
 *
 * Pure function. Same seed yields the same blessing across retries
 * (used for idempotent webhook delivery: a duplicate webhook should
 * insert the same blessing text, not a new variant).
 *
 * Brand-voice rules: no em dashes, "website" not "site/app/platform",
 * affirmative voice, calm register. Hand-written, not generated.
 */

export interface BlessingTemplate {
  /** Stable identifier; written into the companion_turns row for audit. */
  id: string;
  /** The companion turn text. Markdown supported (bold + bulleted lists). */
  text: string;
}

/**
 * The pool. Five variants, each calm and short. Add new entries by
 * appending; do NOT reorder (existing trial_id seeds depend on order
 * for deterministic selection).
 */
export const BLESSING_POOL: BlessingTemplate[] = [
  {
    id: 'blessing-01-its-real',
    text: "It's real now. We made it. Walk around in it for a while; the lamp is set.",
  },
  {
    id: 'blessing-02-quiet',
    text: 'The lamp is set. Quiet your shoulders. We did this together.',
  },
  {
    id: 'blessing-03-its-yours',
    text: "It's yours now, and it's public. That took some courage. Take a breath.",
  },
  {
    id: 'blessing-04-keeper',
    text: "You are a keeper of a small light on the headland. The lamp is lit. Welcome.",
  },
  {
    id: 'blessing-05-room',
    text: "Your website is open. There is a small room on the internet now that holds your work the way you wanted it held. Come back to me anytime.",
  },
];

/**
 * Deterministic selection. Same seed -> same blessing. The seed should
 * be a stable per-keeper string (e.g. trial_id or build_id) so duplicate
 * webhook deliveries pick the same variant.
 *
 * Hash is deliberately simple (sum of char codes mod pool length).
 * Cryptographic hashing is overkill for variant selection.
 */
export function pickBlessing(seed: string): BlessingTemplate {
  if (!seed || typeof seed !== 'string') {
    return BLESSING_POOL[0];
  }
  let sum = 0;
  for (let i = 0; i < seed.length; i += 1) {
    sum = (sum + seed.charCodeAt(i)) % 1000003;
  }
  const idx = sum % BLESSING_POOL.length;
  return BLESSING_POOL[idx];
}

/**
 * Insert a blessing turn into companion_turns. Designed to be called
 * INSIDE an open BEGIN/COMMIT transaction (the webhook handler's), so
 * caller passes the pg client (not pool). Surface is `harbor-blessing`
 * which the table view can style differently if it ever wants to.
 *
 * Idempotent at the SQL level via the WHERE NOT EXISTS guard against
 * the (keeper_id, surface='harbor-blessing') tuple. A duplicate webhook
 * delivery is a no-op (returns 0 rows; selection is still deterministic
 * on opts.seed so the existing row already has the right blessing).
 * The keeper sees the blessing the next time they open /lighthouse/table.
 *
 * Returns the blessing variant + a boolean for whether a new row landed.
 */
export interface InsertBlessingOpts {
  /** keepers.customer_id (UUID) post-promotion. */
  keeperId: string;
  /** Stable seed for deterministic variant pick (use trial_id or build_id). */
  seed: string;
}

export interface InsertBlessingResult {
  blessing: BlessingTemplate;
  /** True iff a new row landed; false if a prior row already existed. */
  inserted: boolean;
}

export async function insertBlessingTurn(
  client: { query: (text: string, params: unknown[]) => Promise<{ rows: unknown[]; rowCount: number | null }> },
  opts: InsertBlessingOpts
): Promise<InsertBlessingResult> {
  const blessing = pickBlessing(opts.seed);
  const result = await client.query(
    `INSERT INTO companion_turns
       (keeper_id, surface, keeper_text, companion_text, rail_flags, created_at)
     SELECT $1::UUID, 'harbor-blessing', NULL, $2, ARRAY[]::TEXT[], NOW()
     WHERE NOT EXISTS (
       SELECT 1 FROM companion_turns
       WHERE keeper_id = $1 AND surface = 'harbor-blessing'
     )
     RETURNING turn_id`,
    [opts.keeperId, blessing.text]
  );
  return {
    blessing,
    inserted: (result.rowCount ?? 0) > 0,
  };
}
