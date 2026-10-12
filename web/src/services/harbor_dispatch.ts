// harbor_dispatch.ts - conversation -> build dispatch for the self-serve routes.
//
// POST /lighthouse/build creates a v2 build from a Harbor conversation
// through this module, for both of its callers:
//   - a cookie-only trial (dispatchHarborTrialBuild): race-safe customer
//     upsert, then the shared steps;
//   - a promoted keeper with an account session (dispatchHarborKeeperBuild):
//     the customer already exists, then the shared steps.
// The shared steps are the Opus extraction, the deterministic translation,
// the barren-spec guard, tier coercion, the warnings kept on the spec, and a
// transaction that inserts the build and stamps the conversation's row under
// a row lock.
//
// Four rules live here:
//   - A build is dispatched only for an account whose email the build engine
//     will accept as the site's admin address: see "The address a build is
//     owned by" below.
//   - A Harbor build is inserted by insertHarborBuild, which sets
//     builds.is_harbor. That column is what makes the customer's own publish
//     of the build (and of every rebuild or revision made from it) require a
//     subscription; see "Harbor origin" in db/schema.sql.
//   - A conversation dispatches one build without an operator. The record of
//     that is handoff_at on the trial or keeper row, which outlives the build:
//     handoff_build_id is ON DELETE SET NULL and builds can be deleted (the
//     admin and ops delete routes), so "no build attached" does not mean
//     "never dispatched".
//   - A dispatch is a build start like a retry, a rebuild or a revision, and
//     counts against the same per-customer limits
//     (services/build-start-limits.ts): see "Build-start limits on a
//     dispatch" below.
//
// The operator route (POST /admin/harbor/:trial_id/dispatch in
// routes/admin.ts) still carries its own, older copy of the steps before the
// insert; it calls insertHarborBuild for the row itself, and like the admin's
// rebuilds it is not subject to the build-start limits. The `source`,
// `allowVerifiedAccount`, `allowRedispatch` and `skipStartLimits` options
// exist so that it can be moved onto dispatchHarborTrialBuild; until then a
// change to the extraction or translation steps has to be made in both places.
//
// The caller resolves the email (self-serve REQUIRES a captured email) and
// does its own post-dispatch hooks; this service does the core dispatch and
// returns the warnings + result.

import type { Pool, PoolClient } from 'pg';
import { extractSpecFromConversation, SpecExtractionError, type SpecExtractionFailure } from './spec_extraction';
import { isEngineSafeAdminEmail, translateExtractedSpecToV1 } from './harbor_to_compose';
import { LlmDailyBudgetError, releaseAnonymousCalls, tryAdmitAnonymousCalls } from './llm-budget';
import { hashUnusablePassword } from '../utils/password-hash';
import {
  ASSISTANT_NOT_CONFIGURED_STATUS,
  AssistantNotConfiguredError,
  assistantNotConfiguredBody,
} from '../utils/assistant';
import {
  BUILD_START_LOCK_SQL,
  buildStartRefusal,
  readBuildStartBudget,
  recordBuildStart,
  type BuildStartKind,
} from './build-start-limits';

export type HarborDispatchErrorCode =
  | 'trial_not_found'
  | 'trial_already_promoted'
  | 'keeper_not_found'
  | 'no_email'
  | 'barren_spec'
  | 'account_exists'
  | 'address_in_use'
  | 'email_not_buildable'
  | 'build_already_used'
  | 'build_limit';

// ---------------------------------------------------------------------------
// The address a build is owned by must be one the build engine accepts
// ---------------------------------------------------------------------------
//
// Before it runs the engine, the worker overwrites the spec's `admin_email`
// with the email of the account that owns the build (queue/worker_pg.py:
// `spec_with_admin["admin_email"] = customer_email`, where customer_email is
// customers.email from the claim's join). The engine refuses a spec whose
// admin_email is not a plain address (`_ADMIN_EMAIL_RE` in compose/build.py:
// no apostrophe, no underscore in the host, ASCII only, at most 254
// characters). Signup and the conversation's email field accept more than
// that. A build owned by such an account is queued, claimed, refused at the
// worker's validation stage ("Spec validation failed") and ends in the
// build-failed email, and here that would be after the extraction was paid
// for. So each dispatch asks isEngineSafeAdminEmail (the web tier's copy of
// that pattern, services/harbor_to_compose.ts) about the owning account's
// address before the extraction and before it writes any row. The operator's
// route in routes/admin.ts makes the same check through this function.
//
// What this does not cover: the retry, rebuild and revision routes in
// routes/builds.ts and the ops API queue a build for whatever account owns
// the row, without this check.

/** True when the engine would accept `email` as the admin address of a build this account owns. */
export function isBuildableOwnerEmail(email: string | null | undefined): boolean {
  return typeof email === 'string' && isEngineSafeAdminEmail(email);
}

/**
 * What an account owner is told when their own account's address is the
 * problem. Never shown for an address a visitor merely typed: the self-serve
 * route answers that in the same words as any other address it cannot use
 * (see POST /lighthouse/build).
 */
export const OWNER_EMAIL_NOT_BUILDABLE_MESSAGE =
  'A website cannot be built for this account: the builder does not accept its email address (it takes letters, digits and . _ % + - before the @, and letters, digits and hyphens after it). Please write to support@craftandship.io.';

function emailNotBuildable(): HarborDispatchError {
  return new HarborDispatchError('email_not_buildable', OWNER_EMAIL_NOT_BUILDABLE_MESSAGE);
}

// ---------------------------------------------------------------------------
// Build-start limits on a dispatch
// ---------------------------------------------------------------------------
//
// services/build-start-limits.ts limits what one customer can put on the
// worker queue: a number of builds queued or in progress at once, and a number
// of starts per rolling 24 hours, counted from the build_start_events ledger
// under a per-customer advisory lock. A dispatch is the first start of all,
// and it reaches the same queue, so it goes through the same module as a
// retry, a rebuild or a revision in routes/builds.ts: the same lock, the same
// counts, the same refusal, and a ledger row of its own. The one-build-per-
// conversation rule above does not make this redundant: many conversations
// can name one address, and each would otherwise queue a build for the same
// customer.
//
// What the limits do not bound: they are per customer, and on the self-serve
// path the customer is whichever address the visitor typed. A visitor who
// gives a different address each time is a different customer each time; what
// bounds that is the per-trial and per-address limit on POST /lighthouse/build
// and the daily model-call budget the extraction draws on.

// ---------------------------------------------------------------------------
// An anonymous dispatch does not attach to an account in use
// ---------------------------------------------------------------------------
//
// A visitor with no account session has proved nothing about the address they
// typed. The account that address names may be unverified and still belong to
// someone else: a payer's account is made at checkout unverified, and a signup
// can sit unverified indefinitely. An account is in use when it has a keeper
// row (a paid conversation) or any build. An anonymous dispatch to such an
// account is refused with address_in_use (409 in dispatchFailureResponse), so
// that no build, no start row and no stamp on the conversation is written for
// the account, and the builds and start limits it already has stay its owner's.
//
// The check runs twice. The early one is made before the extraction is paid
// for. The authoritative one runs inside the insert transaction, under the
// account's build-start lock, so a build that lands on the account while the
// extraction runs is seen there, and the model call has already been paid for.
//
// What this does not cover: an unverified account with neither a keeper row
// nor a build still takes an anonymous dispatch's first build, and that start
// counts against the account. The route answers a visitor in the same words
// for an account in use and for a verified one (see POST /lighthouse/build).

/** True when the account has a keeper row or any build. Reads; takes no lock. */
async function accountInUse(db: Queryable, customerId: string): Promise<boolean> {
  const res = await db.query<{ in_use: boolean }>(
    `SELECT (EXISTS (SELECT 1 FROM keepers WHERE customer_id = $1)
          OR EXISTS (SELECT 1 FROM builds WHERE customer_id = $1)) AS in_use`,
    [customerId]
  );
  return res.rows[0]?.in_use === true;
}

function addressInUse(): HarborDispatchError {
  return new HarborDispatchError(
    'address_in_use',
    'The address belongs to an account that already has a conversation or a build; a build is not attached to it on the strength of a typed address.'
  );
}

/**
 * How a dispatch is labelled in build_start_events.kind. It has a kind of its
 * own, and must keep one: the row carries the dispatched build's id, that
 * build is the root of its lineage, and two readers count rows of one kind
 * for one build id ('retry' rows of a build are its manual retries; 'rebuild'
 * rows under a root are the coordinator's automatic rebuilds of the lineage).
 * A dispatch filed under either of those would be counted as one of them.
 */
export const HARBOR_DISPATCH_START_KIND: BuildStartKind = 'dispatch';

/** Why this customer may not start another build right now, or null. Reads; takes no lock. */
async function startLimitRefusal(db: Queryable, customerId: string): Promise<string | null> {
  // A dispatch is not a retry of an existing build, so no build id is counted.
  return buildStartRefusal(await readBuildStartBudget(db, customerId, null), 'new');
}

export class HarborDispatchError extends Error {
  constructor(
    public code: HarborDispatchErrorCode,
    message: string
  ) {
    super(message);
    this.name = 'HarborDispatchError';
  }
}

// ---------------------------------------------------------------------------
// How a dispatch that did not produce a build is answered
// ---------------------------------------------------------------------------
//
// Three routes dispatch: a visitor's and a keeper's POST /lighthouse/build
// (routes/lighthouse.ts) and the operator's POST /admin/harbor/:trial_id/
// dispatch (routes/admin.ts). All three answer in JSON, and all three answer
// the same failure with the same status, from this one table:
//
//   a refusal of the dispatch itself (HarborDispatchError)   by code, below
//   nothing the extraction may read (nothing_to_extract)      409
//   the model call failed, or its reply is not a spec         502
//   the daily model-call budget is spent                      503, "busy"
//   no model key on this server                               503, as the chat
//
// Anything else (a database error, a broken invariant) is a server fault and
// is not mapped here: dispatchFailureResponse returns null, the route passes
// the error on, and the app's error handler answers a JSON route in JSON
// (utils/json-request.ts).
const DISPATCH_ERROR_STATUS: Record<HarborDispatchErrorCode, number> = {
  trial_not_found: 404,
  keeper_not_found: 404,
  trial_already_promoted: 409,
  no_email: 402,
  barren_spec: 409,
  account_exists: 409,
  address_in_use: 409,
  email_not_buildable: 409,
  build_already_used: 409,
  build_limit: 429,
};

const EXTRACTION_ERROR_STATUS: Record<SpecExtractionFailure, number> = {
  nothing_to_extract: 409,
  model_call_failed: 502,
  unusable_reply: 502,
};

export interface DispatchFailureAnswer {
  status: number;
  body: { error: string; message: string; code?: string };
}

/** The HTTP answer for a dispatch failure that is not a server fault, or null when it is one. */
export function dispatchFailureResponse(err: unknown): DispatchFailureAnswer | null {
  if (err instanceof HarborDispatchError) {
    return { status: DISPATCH_ERROR_STATUS[err.code] ?? 400, body: { error: err.code, message: err.message } };
  }
  if (err instanceof SpecExtractionError) {
    return { status: EXTRACTION_ERROR_STATUS[err.code] ?? 502, body: { error: err.code, message: err.message } };
  }
  if (err instanceof LlmDailyBudgetError) {
    return { status: 503, body: { error: 'busy', message: err.message } };
  }
  if (err instanceof AssistantNotConfiguredError) {
    return { status: ASSISTANT_NOT_CONFIGURED_STATUS, body: assistantNotConfiguredBody() };
  }
  return null;
}

export interface DispatchOptions {
  trialId: string;
  /** Final resolved customer email. Caller resolves it (self-serve: the
   * captured email; admin: captured || ADMIN_EMAIL). Must be non-empty. */
  resolvedEmail: string;
  /** For log attribution: 'self_serve' | 'admin'. */
  source: string;
  /** Self-serve sets true: refuse to create a build from a BARREN_SPEC
   * (empty/garbage conversation) so a self-serve visitor never gets a build made
   * from nothing. Admin sets false (the operator decides). */
  refuseBarrenSpec?: boolean;
  /** The email here is whatever an anonymous visitor typed; nobody has shown
   * they can read that mailbox. By default the build may therefore be attached
   * to a brand-new account or to one that is itself still unverified, but
   * never to an account whose owner has verified the address: that would put
   * a stranger's build (and, after payment, their conversation) into a real
   * customer's account. Set true only where an operator chose the address. */
  allowVerifiedAccount?: boolean;
  /** A trial that has already had a build dispatched (handoff_at is set) and
   * whose build has since been deleted is refused with `build_already_used`.
   * Set true only for an operator, who may dispatch again after deleting a
   * build. */
  allowRedispatch?: boolean;
  /** Leave out the per-customer build-start limits and the ledger row. Set
   * true only for an operator, who is exempt from them the way the admin is
   * in routes/builds.ts. */
  skipStartLimits?: boolean;
  /** The caller is a visitor with no account session. The extraction then has
   * to fit in the part of the daily model-call budget that anonymous callers
   * may use (services/llm-budget.ts), so that anonymous traffic cannot spend
   * the calls held back for customers with an account. The dispatch also
   * refuses an account that is in use (a keeper row or a build), with
   * address_in_use; see "An anonymous dispatch does not attach to an account
   * in use" above. */
  anonymous?: boolean;
}

export interface KeeperDispatchOptions {
  /** customers.id of the keeper (the account session's customer). */
  customerId: string;
  /** Refuse a spec with nothing from the keeper in it; see DispatchOptions. */
  refuseBarrenSpec?: boolean;
}

export interface DispatchResult {
  buildId: string;
  alreadyDispatched: boolean;
  warnings: string[];
  customerId: string;
  tier: string;
}

type Queryable = Pick<PoolClient, 'query'>;

/**
 * The one statement that creates a Harbor build: queued, and marked
 * `is_harbor`. Every dispatch path calls it (the two functions below and the
 * operator route), on the client of the transaction that also stamps the
 * trial or keeper row. Returns the new build id.
 */
export async function insertHarborBuild(
  client: Queryable,
  build: { customerId: string; tier: string; spec: unknown; appName: string }
): Promise<string> {
  const inserted = await client.query<{ id: string }>(
    `INSERT INTO builds (customer_id, tier, status, spec, app_name, is_harbor)
     VALUES ($1, $2, 'queued', $3, $4, TRUE)
     RETURNING id`,
    [build.customerId, build.tier, JSON.stringify(build.spec), build.appName]
  );
  return inserted.rows[0].id;
}

/**
 * Create a queued v2 build from a trial conversation. Idempotent on
 * trial_keepers.handoff_build_id. Throws HarborDispatchError for the expected
 * refusal cases so the caller can map them to HTTP responses.
 */
export async function dispatchHarborTrialBuild(
  pool: Pool,
  opts: DispatchOptions
): Promise<DispatchResult> {
  const { trialId, source } = opts;
  const resolvedEmail = (opts.resolvedEmail || '').trim().toLowerCase();

  // 1. Load the trial: email, handoff state, promotion state, operator overrides.
  const trialRes = await pool.query<{
    trial_id: string;
    email_captured: string | null;
    handoff_build_id: string | null;
    handoff_at: Date | null;
    released_at: Date | null;
    harbor_meta_overrides: Record<string, unknown> | null;
  }>(
    `SELECT trial_id, email_captured, handoff_build_id, handoff_at, released_at,
            harbor_meta_overrides
     FROM trial_keepers
     WHERE trial_id = $1`,
    [trialId]
  );
  if (trialRes.rows.length === 0) {
    throw new HarborDispatchError('trial_not_found', 'Trial not found.');
  }
  const trial = trialRes.rows[0];

  // 2. Idempotency: a build is already attached. Or one was, and is gone: the
  //    conversation has had its build (see the header).
  if (trial.handoff_build_id) {
    return {
      buildId: trial.handoff_build_id,
      alreadyDispatched: true,
      warnings: [],
      customerId: '',
      tier: 'build',
    };
  }
  if (trial.handoff_at && !opts.allowRedispatch) {
    throw buildAlreadyUsed();
  }

  // 3. A promoted trial (released_at) had its turns/memory migrated to keeper
  //    rows; trial-keyed extraction would return nothing.
  if (trial.released_at) {
    throw new HarborDispatchError(
      'trial_already_promoted',
      'This trial has already been promoted to a paid keeper.'
    );
  }

  // 4. Email is the customer identity + (self-serve) the delivery channel.
  if (!resolvedEmail) {
    throw new HarborDispatchError(
      'no_email',
      'No email to attach the build to.'
    );
  }

  // 4a. Everything that can refuse this dispatch on account of the address is
  //    asked BEFORE the extraction is paid for. The first check reads nothing
  //    and decides for good. The other two are repeated after the extraction,
  //    where they are authoritative (the account one against the row the
  //    upsert finds, the limits under the customer's lock); these early reads
  //    only keep a request that is going to be refused from costing a model
  //    call first.
  //      - The engine would refuse the address as the build's admin address
  //        (see "The address a build is owned by" above). The account this
  //        build would be owned by is the one whose email is exactly
  //        resolvedEmail (found or created below), so the answer for the
  //        string is the answer for the account, and no customers row is
  //        created for an address that can never own a build.
  //      - The address already belongs to a verified account: see
  //        allowVerifiedAccount. Refusing here, and not after the extraction,
  //        also means this answer costs the server nothing, so it cannot be
  //        used to spend money one probe at a time.
  //      - That customer is already at a build-start limit.
  if (!isBuildableOwnerEmail(resolvedEmail)) throw emailNotBuildable();
  const known = await pool.query<{ id: string; email_verified: boolean | null }>(
    `SELECT id, email_verified FROM customers WHERE email = $1 LIMIT 1`,
    [resolvedEmail]
  );
  if (known.rows.length > 0) {
    if (known.rows[0].email_verified === true && !opts.allowVerifiedAccount) throw accountExists();
    if (opts.anonymous && (await accountInUse(pool, known.rows[0].id))) throw addressInUse();
    if (!opts.skipStartLimits) {
      const refusal = await startLimitRefusal(pool, known.rows[0].id);
      if (refusal) throw new HarborDispatchError('build_limit', refusal);
    }
  }

  // 4b. An anonymous visitor's extraction must fit in the anonymous share of
  //    the daily model-call budget (see DispatchOptions.anonymous). Taken
  //    last, after every refusal that needs no model, so a refused request
  //    does not use up a slot.
  const admittedAt = Date.now();
  if (opts.anonymous && !tryAdmitAnonymousCalls(1, admittedAt)) {
    throw new LlmDailyBudgetError();
  }

  // 5-9. Extraction, translation, guards, overrides, tier, warnings. The
  //    expensive call (~$0.18); done before the transaction so we don't hold a
  //    DB connection through the network round-trip.
  //
  //    The admission above is for one model call. When this step ends before
  //    the call is made (nothing to extract, no key on this server, the pool
  //    spent, a database error while loading the conversation), the admission
  //    is given back, so requests that cost nothing cannot use up the share
  //    other visitors build from. Once the call is made it stays taken,
  //    whatever comes back: an unusable reply was paid for like a good one,
  //    and a visitor controls the text the model reads.
  let modelCalled = false;
  let prepared: PreparedSpec;
  try {
    prepared = await prepareHarborSpec(pool, {
      conversation: { trialId },
      refuseBarrenSpec: opts.refuseBarrenSpec,
      harborMetaOverrides: trial.harbor_meta_overrides,
      fallbackAppName: `harbor-${trialId.slice(0, 8)}`,
      onModelCall: () => {
        modelCalled = true;
      },
    });
  } catch (prepareErr) {
    if (opts.anonymous && !modelCalled) releaseAnonymousCalls(1, admittedAt);
    throw prepareErr;
  }

  // 10. Race-safe customer upsert, done only now that the spec passed the
  //     guards, so a refused attempt leaves no unverified customers row behind
  //     for whatever address was typed into the email field. ON CONFLICT DO
  //     NOTHING returns no row when a concurrent INSERT already created it, so
  //     re-SELECT. password + name are NOT NULL in the schema; provide an
  //     unguessable ephemeral password (the keeper reaches their account via
  //     magic link / password reset, never this plaintext) and a name derived
  //     from the email local part.
  const ephemeralPassword = await hashUnusablePassword();
  const localPart = resolvedEmail.split('@')[0] || '';
  const ephemeralName = (localPart || 'Keeper').slice(0, 60);
  let customerId: string;
  const upsertRes = await pool.query<{ id: string }>(
    `INSERT INTO customers (email, password, email_verified, name)
     VALUES ($1, $2, FALSE, $3)
     ON CONFLICT (email) DO NOTHING
     RETURNING id`,
    [resolvedEmail, ephemeralPassword, ephemeralName]
  );
  if (upsertRes.rows.length > 0) {
    customerId = upsertRes.rows[0].id;
  } else {
    const existing = await pool.query<{ id: string; email_verified: boolean | null }>(
      `SELECT id, email_verified FROM customers WHERE email = $1 LIMIT 1`,
      [resolvedEmail]
    );
    if (existing.rows.length === 0) {
      throw new Error('customer row vanished between INSERT ON CONFLICT and re-SELECT');
    }
    // The address already has an account. If its owner verified it, a typed
    // email is not enough to attach anything to it (see allowVerifiedAccount).
    // Step 4a normally refuses this before the extraction; this is the same
    // rule applied to the row as it is now, in case the account was verified
    // while the extraction ran.
    if (existing.rows[0].email_verified === true && !opts.allowVerifiedAccount) {
      throw accountExists();
    }
    customerId = existing.rows[0].id;
  }

  // 11. Insert the build and stamp the trial, in one transaction, under the
  //     customer's build-start lock.
  const outcome = await insertBuildAndStamp(pool, TRIAL_HANDOFF, trialId, {
    customerId,
    tier: prepared.tier,
    spec: prepared.spec,
    appName: prepared.appName,
    allowRedispatch: opts.allowRedispatch === true,
    enforceStartLimits: opts.skipStartLimits !== true,
    refuseAccountInUse: opts.anonymous === true,
  });
  if (!outcome.created) {
    return { buildId: outcome.buildId, alreadyDispatched: true, warnings: [], customerId, tier: prepared.tier };
  }

  console.log(
    '[harbor_dispatch] dispatched build=%s for trial=%s source=%s tier=%s warnings=%d',
    outcome.buildId,
    trialId,
    source,
    prepared.tier,
    prepared.warnings.length
  );

  return {
    buildId: outcome.buildId,
    alreadyDispatched: false,
    warnings: prepared.warnings,
    customerId,
    tier: prepared.tier,
  };
}

/**
 * Create a queued v2 build from a promoted keeper's conversation: the keeper
 * path of POST /lighthouse/build. The same steps as the trial path, keyed on
 * the keepers row instead of the trial; the customer already exists, so there
 * is no email to resolve. Idempotent on keepers.handoff_build_id.
 */
export async function dispatchHarborKeeperBuild(
  pool: Pool,
  opts: KeeperDispatchOptions
): Promise<DispatchResult> {
  const { customerId } = opts;

  const keeperRes = await pool.query<{
    customer_id: string;
    handoff_build_id: string | null;
    handoff_at: Date | null;
    email: string | null;
  }>(
    `SELECT k.customer_id, k.handoff_build_id, k.handoff_at, c.email
     FROM keepers k
     JOIN customers c ON c.id = k.customer_id
     WHERE k.customer_id = $1`,
    [customerId]
  );
  if (keeperRes.rows.length === 0) {
    throw new HarborDispatchError('keeper_not_found', 'No keeper for this account.');
  }
  const keeper = keeperRes.rows[0];
  if (keeper.handoff_build_id) {
    return {
      buildId: keeper.handoff_build_id,
      alreadyDispatched: true,
      warnings: [],
      customerId,
      tier: 'build',
    };
  }
  if (keeper.handoff_at) {
    throw buildAlreadyUsed();
  }

  // The keeper's account owns the build, and the worker writes that account's
  // email into the spec exactly as it is stored: it has to be an address the
  // engine accepts (see "The address a build is owned by" above). Asked
  // before the limits and the extraction.
  if (!isBuildableOwnerEmail(keeper.email)) throw emailNotBuildable();

  // Asked before the extraction is paid for; asked again under the lock.
  const earlyRefusal = await startLimitRefusal(pool, customerId);
  if (earlyRefusal) throw new HarborDispatchError('build_limit', earlyRefusal);

  const prepared = await prepareHarborSpec(pool, {
    conversation: { keeperCustomerId: customerId },
    refuseBarrenSpec: opts.refuseBarrenSpec,
    harborMetaOverrides: null,
    fallbackAppName: `harbor-${customerId.slice(0, 8)}`,
  });

  const outcome = await insertBuildAndStamp(pool, KEEPER_HANDOFF, customerId, {
    customerId,
    tier: prepared.tier,
    spec: prepared.spec,
    appName: prepared.appName,
    allowRedispatch: false,
    enforceStartLimits: true,
    refuseAccountInUse: false,
  });
  if (!outcome.created) {
    return { buildId: outcome.buildId, alreadyDispatched: true, warnings: [], customerId, tier: prepared.tier };
  }

  console.log(
    '[harbor_dispatch] dispatched build=%s for keeper=%s source=keeper tier=%s warnings=%d',
    outcome.buildId,
    customerId,
    prepared.tier,
    prepared.warnings.length
  );

  return {
    buildId: outcome.buildId,
    alreadyDispatched: false,
    warnings: prepared.warnings,
    customerId,
    tier: prepared.tier,
  };
}

function buildAlreadyUsed(): HarborDispatchError {
  return new HarborDispatchError(
    'build_already_used',
    'This conversation has already had its website built. Log in to your account to see it, or write to support@craftandship.io.'
  );
}

/**
 * The typed address belongs to a verified account. The message is for logs
 * and operators: the self-serve route answers the visitor in words that do
 * not say an account exists (see POST /lighthouse/build).
 */
function accountExists(): HarborDispatchError {
  return new HarborDispatchError(
    'account_exists',
    'The address belongs to a verified account; a build is not attached to it on the strength of a typed address.'
  );
}

interface PreparedSpec {
  spec: Record<string, unknown>;
  appName: string;
  tier: string;
  warnings: string[];
}

/**
 * From a conversation to the spec that goes on the build row: the Opus
 * extraction, the deterministic translation to v1 shape, the barren-spec
 * guard, the operator's harbor_meta overrides, tier coercion, and the
 * dispatch-time warnings copied onto the spec so they reach the reviewer.
 */
async function prepareHarborSpec(
  pool: Pool,
  input: {
    conversation: { trialId: string } | { keeperCustomerId: string };
    refuseBarrenSpec?: boolean;
    harborMetaOverrides: Record<string, unknown> | null;
    fallbackAppName: string;
    /** Passed through to the extraction; see ExtractSpecOpts.onModelCall. */
    onModelCall?: () => void;
  }
): Promise<PreparedSpec> {
  const extraction = await extractSpecFromConversation({
    pool,
    ...input.conversation,
    onModelCall: input.onModelCall,
  });
  const translation = translateExtractedSpecToV1(
    extraction.spec as Parameters<typeof translateExtractedSpecToV1>[0]
  );
  const v1Spec = translation.spec;
  const warnings = [...extraction.warnings, ...translation.warnings];

  // Self-serve safety: refuse a barren/garbage spec so a build is never made
  // from an empty conversation. Two cases:
  //   - the translator emitted a 'BARREN_SPEC' warning (sections were seen
  //     but 0 were handled), or
  //   - the spec carries nothing from the keeper at all (see
  //     isContentFreeSpec); the translator deliberately lets that through
  //     for operator dispatches, where a human decides.
  // THIN_SPEC (under 25% of sections handled) is only a warning; it is
  // recorded on the build for review and never refuses.
  if (
    input.refuseBarrenSpec &&
    (warnings.some((w) => /BARREN_SPEC/i.test(w)) ||
      isContentFreeSpec(v1Spec as unknown as Record<string, unknown>))
  ) {
    throw new HarborDispatchError(
      'barren_spec',
      'Not enough in the conversation yet to build a real site.'
    );
  }

  // Merge operator harbor_meta_overrides (faithful-copy crawl, archetype
  // hints) into the spec's _harbor_meta. Operator overrides win for same key.
  const overrides = input.harborMetaOverrides;
  if (overrides && typeof overrides === 'object' && !Array.isArray(overrides)) {
    const v1WithMeta = v1Spec as unknown as Record<string, unknown> & {
      _harbor_meta?: Record<string, unknown>;
    };
    const existingMeta =
      v1WithMeta._harbor_meta && typeof v1WithMeta._harbor_meta === 'object'
        ? v1WithMeta._harbor_meta
        : {};
    v1WithMeta._harbor_meta = { ...existingMeta, ...overrides };
    const overrideKeys = Object.keys(overrides).join(', ');
    if (overrideKeys) {
      const note = `dispatch: applied harbor_meta_overrides keys=[${overrideKeys}] from operator pre-dispatch tooling`;
      warnings.push(note);
      translation.warnings.push(note);
    }
  }

  // Tier coercion (worker validates downstream; clear error here is cheaper,
  // and builds.tier has a CHECK constraint that would reject anything else).
  const appName = v1Spec.app_name || input.fallbackAppName;
  const ALLOWED_TIERS = new Set(['build', 'grow', 'scale']);
  const rawTier = (v1Spec.tier ?? 'build').toString();
  const tier = ALLOWED_TIERS.has(rawTier) ? rawTier : 'build';
  const tierNote = `dispatch: Opus returned tier='${rawTier}'; coerced to 'build'`;
  if (rawTier !== tier) warnings.push(tierNote);

  // Persist dispatch-time warnings into builds.spec so they survive into
  // review surfaces.
  const v1WithWarnings = v1Spec as unknown as Record<string, unknown> & {
    _extraction_warnings?: unknown;
    _translation_warnings?: unknown;
  };
  const existingExtraction = Array.isArray(v1WithWarnings._extraction_warnings)
    ? (v1WithWarnings._extraction_warnings as unknown[])
    : [];
  const existingTranslation = Array.isArray(v1WithWarnings._translation_warnings)
    ? (v1WithWarnings._translation_warnings as unknown[])
    : [];
  v1WithWarnings._extraction_warnings = [...existingExtraction, ...extraction.warnings];
  v1WithWarnings._translation_warnings = [
    ...existingTranslation,
    ...translation.warnings,
    ...(rawTier !== tier ? [tierNote] : []),
  ];

  return { spec: v1WithWarnings, appName, tier, warnings };
}

/**
 * The two rows a dispatch can be recorded on, with the statements that lock
 * and stamp them. `handoff_build_id IS NULL` in the stamp is belt and braces:
 * the row is already locked when it runs.
 */
interface HandoffStatements {
  lock: string;
  stamp: string;
  missing: () => HarborDispatchError;
}
const TRIAL_HANDOFF: HandoffStatements = {
  lock: `SELECT handoff_build_id, handoff_at FROM trial_keepers WHERE trial_id = $1 FOR UPDATE`,
  stamp: `UPDATE trial_keepers
         SET handoff_build_id = $1, handoff_at = NOW()
         WHERE trial_id = $2 AND handoff_build_id IS NULL`,
  missing: () => new HarborDispatchError('trial_not_found', 'Trial not found.'),
};
const KEEPER_HANDOFF: HandoffStatements = {
  lock: `SELECT handoff_build_id, handoff_at FROM keepers WHERE customer_id = $1 FOR UPDATE`,
  stamp: `UPDATE keepers
         SET handoff_build_id = $1, handoff_at = NOW(), updated_at = NOW()
         WHERE customer_id = $2 AND handoff_build_id IS NULL`,
  missing: () => new HarborDispatchError('keeper_not_found', 'No keeper for this account.'),
};

/**
 * Insert the build and stamp the trial or keeper row in one transaction, so a
 * partial failure cannot leave a queued build without its stamp (which would
 * let a second click create a second one).
 *
 * The idempotency reads the callers do first are unlocked, and two concurrent
 * dispatches for the same conversation (a double click, or admin plus
 * self-serve) can both have passed them and both run the Opus extraction.
 * FOR UPDATE serializes the two transactions here so exactly one inserts a
 * build; the loser sees the winner's stamp and returns it (`created: false`).
 * The same locked read applies the one-build rule: a row whose build was
 * dispatched and later deleted is refused unless `allowRedispatch`.
 *
 * The build-start limits are applied in the same transaction (see the top of
 * this file). The customer's advisory lock is taken first, as its own
 * statement, so the counts that follow see whatever the previous holder
 * committed: two conversations dispatching for one customer at once, or a
 * dispatch racing a rebuild in routes/builds.ts, cannot both read "one build
 * active" and both queue another. It is taken before the row lock because
 * routes/builds.ts takes it before touching any row; the payment webhook
 * locks a build and then the trial row and never waits for this lock, so no
 * cycle is possible. An accepted dispatch writes its ledger row here too, and
 * a refusal rolls everything back.
 */
async function insertBuildAndStamp(
  pool: Pool,
  handoff: HandoffStatements,
  ownerId: string,
  build: {
    customerId: string;
    tier: string;
    spec: unknown;
    appName: string;
    allowRedispatch: boolean;
    enforceStartLimits: boolean;
    /** An anonymous dispatch: refuse an account that is in use (see "An anonymous dispatch"). */
    refuseAccountInUse: boolean;
  }
): Promise<{ buildId: string; created: boolean }> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    if (build.enforceStartLimits) {
      await client.query(BUILD_START_LOCK_SQL, [build.customerId]);
    }
    const lockRes = await client.query<{ handoff_build_id: string | null; handoff_at: Date | null }>(
      handoff.lock,
      [ownerId]
    );
    if (lockRes.rows.length === 0) throw handoff.missing();
    const { handoff_build_id: existingHandoff, handoff_at: handoffAt } = lockRes.rows[0];
    if (existingHandoff) {
      await client.query('COMMIT');
      return { buildId: existingHandoff, created: false };
    }
    if (handoffAt && !build.allowRedispatch) throw buildAlreadyUsed();
    if (build.refuseAccountInUse && (await accountInUse(client, build.customerId))) throw addressInUse();

    if (build.enforceStartLimits) {
      const refusal = await startLimitRefusal(client, build.customerId);
      if (refusal) throw new HarborDispatchError('build_limit', refusal);
    }

    const buildId = await insertHarborBuild(client, build);
    await client.query(handoff.stamp, [buildId, ownerId]);
    if (build.enforceStartLimits) {
      await recordBuildStart(client, build.customerId, buildId, HARBOR_DISPATCH_START_KIND);
    }
    await client.query('COMMIT');
    return { buildId, created: true };
  } catch (txErr) {
    await client.query('ROLLBACK').catch(() => undefined);
    throw txErr;
  } finally {
    client.release();
  }
}

const CONTENT_SEED_KEYS = [
  'hero_tagline',
  'hero_description',
  'about_title',
  'about_body',
  'site_name',
] as const;

/** The extraction prompt asks for this placeholder when no offering was named. */
const PLACEHOLDER_OFFERING_TITLE = /^get in touch$/i;

/**
 * True when the translated spec carries nothing the keeper actually said: no
 * hero / about / site-name copy, no named offering other than the "Get in
 * touch" placeholder, no testimonial, no booking path. Such a spec builds an
 * archetype-default site that has no relationship to the keeper's work.
 *
 * Deliberately narrow (a single real signal clears it); contact details alone
 * do not count. Used only for self-serve dispatch.
 */
export function isContentFreeSpec(spec: Record<string, unknown>): boolean {
  const hasText = (v: unknown): boolean => typeof v === 'string' && v.trim().length > 0;
  const seed = spec._seed_settings;
  if (seed && typeof seed === 'object') {
    const seedObj = seed as Record<string, unknown>;
    if (CONTENT_SEED_KEYS.some((k) => hasText(seedObj[k]))) return false;
  }
  const meta = spec._harbor_meta;
  if (meta && typeof meta === 'object') {
    const metaObj = meta as Record<string, unknown>;
    const services = Array.isArray(metaObj.services) ? metaObj.services : [];
    for (const svc of services) {
      const title = svc && typeof svc === 'object' ? (svc as Record<string, unknown>).title : null;
      if (hasText(title) && !PLACEHOLDER_OFFERING_TITLE.test((title as string).trim())) return false;
    }
    if (Array.isArray(metaObj.testimonials) && metaObj.testimonials.length > 0) return false;
    if (Array.isArray(metaObj.booking_intents) && metaObj.booking_intents.length > 0) return false;
  }
  return true;
}
