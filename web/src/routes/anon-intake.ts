import { AssistantNotConfiguredError, respondAssistantNotConfigured } from '../utils/assistant';
import { Router, Request, Response, NextFunction } from 'express';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import { track } from '../services/funnel-events';
import { LlmDailyBudgetError, tryAdmitAnonymousCalls } from '../services/llm-budget';
import { verifyHmacToken } from '../middleware/auth';
import { processIntakeMessage, generateInitialGreeting, generateAnonSpecPreview, CircuitBreakerOpenError, AnonSpecPreview, ANON_STATIC_GREETING } from '../services/claude';
import {
  generateAnonSessionId,
  createAnonDraft,
  getAnonDraftByBuildId,
} from '../services/anon-intake';
import {
  anonDraftCreateRateLimit,
  anonMessageRateLimit,
  anonDailyTurnRateLimit,
  anonGlobalDailyTurnRateLimit,
} from '../middleware/anon-rate-limit';
import { generateCsrfToken } from '../middleware/auth';
import { IntakeMessage } from '../types';

// Anonymous intake preview routes. Visitors can start a chat and send a
// limited number of messages before being prompted to sign up. State is
// tied to a session cookie via req.session.anonSessionId (reusing the
// existing express-session store: no cookie-parser dependency).
//
// Mounted at /try in app.ts to avoid colliding with the authenticated
// /intake router. On turn cap the server returns
// { signupRequired: true } and the client navigates to /signup, where
// the existing POST /signup flow claims the draft via
// claimAnonDraftBySessionId (anon-intake-signup-migration item).

const router = forwardAsyncErrors(Router());

// GET /try: renders the anonymous chat page. No build row or API call
// happens here; bots and crawlers get a static HTML page that costs
// nothing. Build creation is deferred to POST /try/start (rate-limited).
//
// When CAS_HARBOR_ENABLED=true, /try is the legacy intake; the canonical
// experience is the harbor companion at /lighthouse/table (no message cap;
// shared with logged-in users; persistent memory). Redirect there.
router.get('/', async (req: Request, res: Response) => {
  try {
    if (process.env.CAS_HARBOR_ENABLED === 'true') {
      // Funnel: the homepage "Begin a conversation" CTA points at /try, which
      // redirects to Haven. Track the click here (the top of the activation
      // funnel: cta_click_begin -> table_view -> first_message).
      track(pool, { name: 'cta_click_begin', req });
      res.redirect(302, '/lighthouse/table');
      return;
    }

    // Reuse an in-progress draft for this session if one exists.
    if (req.session.anonSessionId && req.session.anonBuildId) {
      const existing = await getAnonDraftByBuildId(
        req.session.anonBuildId,
        req.session.anonSessionId
      );
      if (existing && existing.status === 'intake') {
        res.redirect(`/try/${existing.id}`);
        return;
      }
    }

    // No draft yet: render with a static greeting, no DB or API calls.
    const csrfToken = generateCsrfToken(req);
    res.render('intake-anon', {
      title: 'Try Craft & Ship',
      buildId: null,
      messages: [{ role: 'assistant', content: ANON_STATIC_GREETING, created_at: new Date() }],
      turnCount: 0,
      turnCap: ANON_TURN_CAP,
      csrfToken,
      specPreview: null,
      funnelMode: true,
    });
  } catch (err) {
    console.error('anon-intake: GET /try failed:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to start preview. Please try again.' });
  }
});

// GET /try/:buildId, render the anonymous chat UI. Session ownership is
// verified; unknown buildIds or wrong sessions get a 404. When the harbor
// flag is on, redirect to the canonical /lighthouse/table surface so any
// stale /try/:buildId bookmark still lands on Haven.
router.get('/:buildId', async (req: Request, res: Response) => {
  if (process.env.CAS_HARBOR_ENABLED === 'true') {
    res.redirect(302, '/lighthouse/table');
    return;
  }
  const { buildId } = req.params;

  if (!req.session.anonSessionId) {
    res.redirect('/try');
    return;
  }

  try {
    const draft = await getAnonDraftByBuildId(buildId, req.session.anonSessionId);
    if (!draft || draft.status !== 'intake') {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const messagesResult = await pool.query<IntakeMessage>(
      'SELECT role, content, created_at FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
      [buildId]
    );
    const messages = messagesResult.rows;
    const turnCount = messages.filter((m) => m.role === 'user').length;
    const csrfToken = generateCsrfToken(req);

    // Pull the latest spec preview if one has been generated for this
    // draft yet. Stored on builds.spec.preview by POST /:buildId/message.
    const specRow = await pool.query<{ spec: { preview?: AnonSpecPreview } | null }>(
      'SELECT spec FROM builds WHERE id = $1',
      [buildId]
    );
    const specPreview = specRow.rows[0]?.spec?.preview ?? null;

    res.render('intake-anon', {
      title: 'Try Craft & Ship',
      buildId: draft.id,
      messages,
      turnCount,
      turnCap: ANON_TURN_CAP,
      csrfToken,
      specPreview,
      funnelMode: true,
    });
  } catch (err) {
    console.error('anon-intake: GET /try/:buildId failed:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load preview.' });
  }
});

// Turn cap: how many user messages an anon visitor can send before being
// prompted to sign up. The legacy 3-turn cap was raised 2026-04-28 because
// it cut keepers off mid-thought. The Haven flow at /lighthouse/table is
// the canonical experience and has its own email-gate at turn 22 instead;
// this legacy path only runs when CAS_HARBOR_ENABLED is unset, and the
// raised cap keeps it from being the bottleneck.
const ANON_TURN_CAP = 100;

// Payload cap: tighter than the authenticated flow (2000) because anon
// traffic is untrusted.
const ANON_MESSAGE_MAX_CHARS = 1000;

interface StartResponse {
  buildId: string;
  greeting: string;
  seeded?: boolean;
}

/**
 * With Harbor enabled this intake is retired. GET /try and GET /try/:buildId
 * redirect to the Haven conversation, so no page the server renders posts to
 * the two routes below, and a POST is answered 404 like any other path that
 * does not exist. It sits in front of the rate limiters and everything else:
 * a refused request draws on no limiter, reads no row and calls no model.
 * (A route with no page still answers anyone who can write a fetch, and each
 * request here costs up to two model calls out of the daily budget that
 * customers' calls share.) The `redirect` field tells a page that was loaded
 * before the switch where the conversation is now.
 */
function refuseWhenHarborEnabled(_req: Request, res: Response, next: NextFunction): void {
  if (process.env.CAS_HARBOR_ENABLED === 'true') {
    res.status(404).json({ error: 'Not found.', redirect: '/lighthouse/table' });
    return;
  }
  next();
}

/** The answer when anonymous visitors have used their share of the daily model-call budget. */
function respondAnonymousBudgetSpent(res: Response): void {
  res.status(503).json({ error: new LlmDailyBudgetError().message });
}

router.post('/start', refuseWhenHarborEnabled, anonDraftCreateRateLimit, anonGlobalDailyTurnRateLimit, async (req: Request, res: Response) => {
  const csrfToken = req.headers['x-csrf-token'] as string;

  if (!csrfToken || !verifyHmacToken(csrfToken, req)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }

  // Optional seed message: when the homepage hero input posts here, the
  // visitor's first prompt comes along for the ride. We skip the canned
  // greeting and process their message as turn 1 in a single round-trip,
  // so by the time they land on /try/:buildId the conversation is already
  // flowing. Empty or oversized input falls back to the plain greeting.
  const rawSeed = typeof req.body?.message === 'string' ? req.body.message : '';
  const seedMessage = rawSeed.trim().slice(0, ANON_MESSAGE_MAX_CHARS);
  const hasSeed = seedMessage.length > 0;

  // The most this request can ask of the model: the reply and the spec
  // preview for a seed message, or one generated greeting without one. It has
  // to fit in the anonymous share of the daily budget (services/llm-budget.ts)
  // before anything is created. A request that turns out to need fewer calls
  // (an existing draft returned as it is) still counts in full; the share is
  // an upper bound on what anonymous visitors can cause, not a meter.
  if (!tryAdmitAnonymousCalls(hasSeed ? 2 : 1)) {
    respondAnonymousBudgetSpent(res);
    return;
  }

  // If the visitor already has a live anon draft in their session,
  // return it idempotently instead of creating a second one. Prevents
  // accidental draft spam from double-clicks or retries.
  //
  // If a seed message arrives on an existing-draft request, process it
  // as the next turn on the existing draft (so the visitor's fresh
  // homepage prompt isn't silently dropped). If the existing draft is
  // already at the turn cap we fall through and return the draft without
  // processing the seed: the chat UI will show the signup wall.
  if (req.session.anonSessionId && req.session.anonBuildId) {
    try {
      const existing = await getAnonDraftByBuildId(
        req.session.anonBuildId,
        req.session.anonSessionId
      );
      if (existing && existing.status === 'intake') {
        const greetingRow = await pool.query<{ content: string }>(
          `SELECT content FROM intake_messages
           WHERE build_id = $1 AND role = 'assistant'
           ORDER BY created_at ASC LIMIT 1`,
          [existing.id]
        );

        if (hasSeed) {
          try {
            const histRes = await pool.query<IntakeMessage>(
              'SELECT role, content, created_at FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
              [existing.id]
            );
            const history = histRes.rows as IntakeMessage[];
            while (history.length > 0 && history[history.length - 1].role === 'user') {
              history.pop();
            }
            const turnCount = history.filter((m) => m.role === 'user').length;
            if (turnCount < ANON_TURN_CAP) {
              const updatedHistory: IntakeMessage[] = [
                ...history,
                { role: 'user', content: seedMessage, created_at: new Date() } as IntakeMessage,
              ];
              const [response, specPreview] = await Promise.all([
                processIntakeMessage(seedMessage, history, turnCount, { anonymous: true }),
                generateAnonSpecPreview(updatedHistory),
              ]);
              await pool.query(
                'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
                [existing.id, 'user', seedMessage]
              );
              await pool.query(
                'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
                [existing.id, 'assistant', response.reply]
              );
              if (specPreview) {
                await pool.query(
                  `UPDATE builds SET spec = COALESCE(spec, '{}'::jsonb) || $1::jsonb WHERE id = $2`,
                  [JSON.stringify({ preview: specPreview }), existing.id]
                );
              }
            }
          } catch (seedErr) {
            if (seedErr instanceof AssistantNotConfiguredError) {
              respondAssistantNotConfigured(res);
              return;
            }
            console.error('anon-intake: seed on existing draft failed:', (seedErr as Error).message);
          }
        }

        const payload: StartResponse = {
          buildId: existing.id,
          greeting: greetingRow.rows[0]?.content ?? '',
          seeded: hasSeed,
        };
        res.json(payload);
        return;
      }
    } catch (err) {
      // Fall through to creating a fresh draft. Log and continue.
      console.error('anon-intake: session lookup failed, creating fresh draft:', (err as Error).message);
    }
  }

  try {
    const sessionId = generateAnonSessionId();
    const { buildId } = await createAnonDraft(sessionId);

    // When a seed message is present (the common case; user typed in
    // the /try chat or the homepage hero), use the static greeting to
    // save an API call and keep the greeting consistent with what the
    // user already saw on GET /try. Only call the API for the rare
    // no-seed fallback (empty body somehow reaching here).
    const greeting = hasSeed
      ? ANON_STATIC_GREETING
      : await generateInitialGreeting();

    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'assistant', greeting]
    );

    // If a seed message came in, process it as turn 1 in the same
    // request. Failures here are non-fatal: we still return the fresh
    // draft so the client can navigate and retry with the normal
    // /:buildId/message endpoint.
    let seededOk = false;
    if (hasSeed) {
      try {
        const history: IntakeMessage[] = [
          { role: 'assistant', content: greeting, created_at: new Date() } as IntakeMessage,
        ];
        const updatedHistory: IntakeMessage[] = [
          ...history,
          { role: 'user', content: seedMessage, created_at: new Date() } as IntakeMessage,
        ];
        const [response, specPreview] = await Promise.all([
          processIntakeMessage(seedMessage, history, 0, { anonymous: true }),
          generateAnonSpecPreview(updatedHistory),
        ]);
        await pool.query(
          'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
          [buildId, 'user', seedMessage]
        );
        await pool.query(
          'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
          [buildId, 'assistant', response.reply]
        );
        if (specPreview) {
          await pool.query(
            `UPDATE builds SET spec = COALESCE(spec, '{}'::jsonb) || $1::jsonb WHERE id = $2`,
            [JSON.stringify({ preview: specPreview }), buildId]
          );
        }
        seededOk = true;
      } catch (seedErr) {
        if (seedErr instanceof AssistantNotConfiguredError) {
          // Say so now, on the page the visitor is looking at, instead of
          // navigating them to a chat that has silently dropped their message.
          respondAssistantNotConfigured(res);
          return;
        }
        console.error('anon-intake: seed message processing failed:', (seedErr as Error).message);
      }
    }

    // Persist on the session so subsequent /try/:buildId/message calls
    // can verify ownership without trusting the URL alone.
    req.session.anonSessionId = sessionId;
    req.session.anonBuildId = buildId;

    req.session.save((err) => {
      if (err) {
        console.error('anon-intake: session save failed:', err.message);
        res.status(500).json({ error: 'Failed to start intake. Please try again.' });
        return;
      }
      const payload: StartResponse = { buildId, greeting, seeded: seededOk };
      res.json(payload);
    });
  } catch (err) {
    console.error('anon-intake: start failed:', (err as Error).message);
    res.status(500).json({ error: 'Failed to start intake. Please try again.' });
  }
});

router.post(
  '/:buildId/message',
  refuseWhenHarborEnabled,
  anonMessageRateLimit,
  anonDailyTurnRateLimit,
  anonGlobalDailyTurnRateLimit,
  async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const { message } = req.body as { message: string };
  const csrfToken = req.headers['x-csrf-token'] as string;

  if (!csrfToken || !verifyHmacToken(csrfToken, req)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }

  if (!req.session.anonSessionId) {
    res.status(401).json({ error: 'No anonymous session. Refresh and start over.' });
    return;
  }

  if (!message || typeof message !== 'string' || message.trim().length === 0) {
    res.status(400).json({ error: 'Message is required.' });
    return;
  }

  const sanitizedMessage = message.trim().slice(0, ANON_MESSAGE_MAX_CHARS);

  try {
    const draft = await getAnonDraftByBuildId(buildId, req.session.anonSessionId);

    if (!draft) {
      res.status(404).json({ error: 'Draft not found or already claimed.' });
      return;
    }

    if (draft.status !== 'intake') {
      res.status(409).json({ error: 'This draft is no longer in intake status.' });
      return;
    }

    const historyResult = await pool.query(
      'SELECT role, content, created_at FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
      [buildId]
    );

    const history = historyResult.rows as IntakeMessage[];

    // Defensive: drop trailing user messages from a prior failed call
    // so retries don't double-insert the same user turn.
    while (history.length > 0 && history[history.length - 1].role === 'user') {
      history.pop();
    }

    const turnCount = history.filter((m) => m.role === 'user').length;

    // Turn cap check: block BEFORE making the Claude API call so we
    // never spend tokens on a request that can't be persisted.
    if (turnCount >= ANON_TURN_CAP) {
      res.status(200).json({
        signupRequired: true,
        buildId,
        reason: `Sign up to continue the conversation. Anonymous previews are limited to ${ANON_TURN_CAP} messages.`,
      });
      return;
    }

    // Two model calls follow (the reply and the spec preview). They have to
    // fit in the anonymous share of the daily budget; see POST /start.
    if (!tryAdmitAnonymousCalls(2)) {
      respondAnonymousBudgetSpent(res);
      return;
    }

    // Call Claude BEFORE persisting the user message. If the API call
    // fails, the message is not saved, keeping the conversation clean
    // and retryable. (Mirrors the authenticated intake flow.)
    //
    // Run the spec-preview summary in parallel: it's a secondary Haiku
    // call whose result powers the "proof the AI understood you" panel
    // on the signup wall. Never allowed to fail the main flow; the
    // helper returns null on any error.
    const updatedHistory: IntakeMessage[] = [
      ...history,
      { role: 'user', content: sanitizedMessage, created_at: new Date() } as IntakeMessage,
    ];
    const [response, specPreview] = await Promise.all([
      processIntakeMessage(sanitizedMessage, history, turnCount, { anonymous: true }),
      generateAnonSpecPreview(updatedHistory),
    ]);

    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'user', sanitizedMessage]
    );
    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'assistant', response.reply]
    );

    // Persist the preview on builds.spec so GET /try/:buildId and
    // GET /signup can read it without re-calling Haiku. Non-blocking:
    // if the preview is null (API hiccup), leave the existing one alone.
    if (specPreview) {
      await pool.query(
        `UPDATE builds SET spec = COALESCE(spec, '{}'::jsonb) || $1::jsonb WHERE id = $2`,
        [JSON.stringify({ preview: specPreview }), buildId]
      );
    }

    // Signal to the client when the next turn will hit the cap, so the
    // UI can start showing "one more message" messaging proactively.
    const nextTurnCount = turnCount + 1;
    const signupRequiredNext = nextTurnCount >= ANON_TURN_CAP;

    res.json({
      reply: response.reply,
      turnCount: nextTurnCount,
      turnsRemaining: Math.max(0, ANON_TURN_CAP - nextTurnCount),
      signupRequiredNext,
      specPreview,
    });
  } catch (err) {
    console.error('anon-intake: message failed:', (err as Error).message);
    if (err instanceof AssistantNotConfiguredError) {
      respondAssistantNotConfigured(res);
    } else if (err instanceof CircuitBreakerOpenError) {
      res.status(503).json({ error: (err as Error).message });
    } else {
      res.status(500).json({ error: 'Failed to process message. Please try again.' });
    }
  }
  }
);

export default router;
