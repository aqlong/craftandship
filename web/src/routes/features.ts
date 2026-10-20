import { respondAssistantNotConfigured } from '../utils/assistant';
import { armingSwitch } from '../utils/env-switch';
import { Router, Request, Response } from 'express';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import { boundedRateLimit, customerOrAddressKey } from '../middleware/companion-rate-limit';
import { requireAuth, generateCsrfToken, validateCsrfToken, verifyHmacToken, isAdminSession, isDevOrTestEnv } from '../middleware/auth';
import { processFeatureMessage, generateFeatureGreeting } from '../services/claude';
import { FEATURE_HARD_TURN_CAP } from '../services/llm-budget';
import { customerLlmDailyLimit } from '../middleware/llm-rate-limit';
import { FeatureMessage, FeatureRequest, AppSpec, Build } from '../types';
import { sendBuildQueuedEmail } from '../services/email';

const router = forwardAsyncErrors(Router());

const featureRateLimit = boundedRateLimit({
  windowMs: 60 * 1000,
  max: 30,
  message: { error: 'Too many messages. Please wait a moment before sending again.' },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: customerOrAddressKey,
});

const FEATURE_STATUS_LABELS: Record<string, string> = {
  drafting: 'Drafting',
  scoped: 'Scoped',
  payment_pending: 'Awaiting Payment',
  queued: 'Queued',
  building: 'Building',
  live: 'Complete',
  failed: 'Failed',
};

function isAdmin(req: Request): boolean {
  return isAdminSession(req);
}

/**
 * Whether this caller may use the feature-payment bypass. STRIPE_BYPASS is an
 * arming switch, read by the one rule for those (armingSwitch in
 * utils/env-switch.ts): only true, 1, yes, on or enabled turn it on. `false`,
 * `0`, `no`, `off`, an empty value, an unset one and any other text leave it
 * off, so a value written to say "no" can never be the one that skips payment.
 * With it on and NODE_ENV "development" or "test", every logged-in customer
 * may use it on their own feature requests; anywhere else (production,
 * staging, NODE_ENV unset) the caller must also be the admin.
 */
function paymentBypassAllowed(admin: boolean): boolean {
  if (!armingSwitch(process.env.STRIPE_BYPASS)) return false;
  return isDevOrTestEnv() || admin;
}

router.get('/projects/:buildId/features', requireAuth, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const admin = isAdmin(req);

  try {
    const query = admin
      ? "SELECT id, app_name, status, live_url, preview_url FROM builds WHERE id = $1 AND status IN ('live', 'preview')"
      : "SELECT id, app_name, status, live_url, preview_url FROM builds WHERE id = $1 AND customer_id = $2 AND status IN ('live', 'preview')";
    const params = admin ? [buildId] : [buildId, req.session.customerId];
    const buildResult = await pool.query(query, params);

    if (buildResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = buildResult.rows[0] as Build;

    const featuresQuery = admin
      ? 'SELECT id, status, description, scope_summary, estimated_cost, created_at, updated_at FROM feature_requests WHERE build_id = $1 ORDER BY created_at DESC'
      : 'SELECT id, status, description, scope_summary, estimated_cost, created_at, updated_at FROM feature_requests WHERE build_id = $1 AND customer_id = $2 ORDER BY created_at DESC';
    const featuresParams = admin ? [buildId] : [buildId, req.session.customerId];
    const featuresResult = await pool.query(featuresQuery, featuresParams);

    const csrfToken = generateCsrfToken(req);

    res.render('features', {
      title: `Features - ${build.app_name || 'Project'}`,
      build,
      features: featuresResult.rows as FeatureRequest[],
      statusLabels: FEATURE_STATUS_LABELS,
      csrfToken,
    });
  } catch (err) {
    console.error('Error loading features list:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load features.' });
  }
});

// Each new feature request inserts a row and asks the model for a greeting.
router.post('/projects/:buildId/features/new', requireAuth, validateCsrfToken, customerLlmDailyLimit, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const admin = isAdmin(req);

  try {
    const query = admin
      ? "SELECT id, spec, customer_id FROM builds WHERE id = $1 AND status IN ('live', 'preview')"
      : "SELECT id, spec, customer_id FROM builds WHERE id = $1 AND customer_id = $2 AND status IN ('live', 'preview')";
    const params = admin ? [buildId] : [buildId, req.session.customerId];
    const buildResult = await pool.query(query, params);

    if (buildResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = buildResult.rows[0] as { id: string; spec: AppSpec | null; customer_id: string };
    const appSpec = build.spec || { app_name: 'unknown', description: 'No spec available' } as AppSpec;

    // The request belongs to the build's owner. When the admin opens one on a
    // customer's behalf the session user is the admin, who must not end up as
    // its owner (the customer would never see it).
    const result = await pool.query(
      `INSERT INTO feature_requests (build_id, customer_id, status)
       VALUES ($1, $2, 'drafting')
       RETURNING id`,
      [buildId, build.customer_id]
    );

    const featureId = (result.rows[0] as { id: string }).id;

    const greeting = await generateFeatureGreeting(appSpec);

    await pool.query(
      'INSERT INTO feature_messages (feature_request_id, role, content) VALUES ($1, $2, $3)',
      [featureId, 'assistant', greeting]
    );

    res.redirect(`/features/${featureId}`);
  } catch (err) {
    console.error('Error creating feature request:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to start feature request.' });
  }
});

router.get('/features/:featureId', requireAuth, async (req: Request, res: Response) => {
  const { featureId } = req.params;
  const admin = isAdmin(req);

  try {
    const query = admin
      ? 'SELECT * FROM feature_requests WHERE id = $1'
      : 'SELECT * FROM feature_requests WHERE id = $1 AND customer_id = $2';
    const params = admin ? [featureId] : [featureId, req.session.customerId];
    const featureResult = await pool.query(query, params);

    if (featureResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const feature = featureResult.rows[0] as FeatureRequest;

    const buildResult = await pool.query(
      'SELECT id, app_name, live_url FROM builds WHERE id = $1',
      [feature.build_id]
    );

    const build = buildResult.rows[0] as Build;

    const messagesResult = await pool.query(
      'SELECT * FROM feature_messages WHERE feature_request_id = $1 ORDER BY created_at ASC',
      [featureId]
    );

    const csrfToken = generateCsrfToken(req);

    res.render('feature-chat', {
      title: `Feature Request - ${build.app_name || 'Project'}`,
      feature,
      build,
      messages: messagesResult.rows as FeatureMessage[],
      csrfToken,
      scopeComplete: feature.status === 'scoped' || feature.status === 'payment_pending',
      // Shows the "skip payment" control. The POST re-checks the same rule.
      devMode: paymentBypassAllowed(admin),
    });
  } catch (err) {
    console.error('Error loading feature chat:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load feature request.' });
  }
});

router.post('/features/:featureId/message', requireAuth, featureRateLimit, customerLlmDailyLimit, async (req: Request, res: Response) => {
  const { featureId } = req.params;
  const { message } = req.body as { message?: unknown };
  const csrfToken = req.headers['x-csrf-token'] as string;
  const admin = isAdmin(req);

  if (!csrfToken || !verifyHmacToken(csrfToken, req)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }

  if (typeof message !== 'string' || message.trim().length === 0) {
    res.status(400).json({ error: 'Message is required.' });
    return;
  }

  const sanitizedMessage = message.trim().slice(0, 2000);

  try {
    const query = admin
      ? "SELECT fr.*, b.spec FROM feature_requests fr JOIN builds b ON fr.build_id = b.id WHERE fr.id = $1 AND fr.status = 'drafting'"
      : "SELECT fr.*, b.spec FROM feature_requests fr JOIN builds b ON fr.build_id = b.id WHERE fr.id = $1 AND fr.customer_id = $2 AND fr.status = 'drafting'";
    const params = admin ? [featureId] : [featureId, req.session.customerId];
    const featureResult = await pool.query(query, params);

    if (featureResult.rows.length === 0) {
      res.status(404).json({ error: 'Feature request not found or not in drafting status.' });
      return;
    }

    const feature = featureResult.rows[0] as FeatureRequest & { spec: AppSpec | null };
    const appSpec = feature.spec || { app_name: 'unknown', description: 'No spec available' } as AppSpec;

    const historyResult = await pool.query(
      'SELECT role, content, created_at FROM feature_messages WHERE feature_request_id = $1 ORDER BY created_at ASC',
      [featureId]
    );

    const history = historyResult.rows as FeatureMessage[];

    // Same contract as the intake and revision chats: drop a trailing user
    // message left by an earlier failed call, then call Claude BEFORE saving
    // anything. This handler used to save the user message first, so one failed
    // API call left an unanswered message in the transcript and the retry sent
    // Claude two user turns in a row.
    while (history.length > 0 && history[history.length - 1].role === 'user') {
      history.pop();
    }
    const turnCount = history.filter((m) => m.role === 'user').length;
    if (turnCount >= FEATURE_HARD_TURN_CAP) {
      res.status(409).json({
        error:
          'This conversation has reached its maximum length. Please start a new feature request, or write to support@craftandship.io.',
      });
      return;
    }

    const response = await processFeatureMessage(sanitizedMessage, history, turnCount, appSpec);

    await pool.query(
      'INSERT INTO feature_messages (feature_request_id, role, content) VALUES ($1, $2, $3)',
      [featureId, 'user', sanitizedMessage]
    );
    await pool.query(
      'INSERT INTO feature_messages (feature_request_id, role, content) VALUES ($1, $2, $3)',
      [featureId, 'assistant', response.reply]
    );

    if (response.scopeComplete && response.scope) {
      await pool.query(
        `UPDATE feature_requests
         SET status = 'scoped',
             description = $1,
             scope_summary = $2,
             estimated_cost = $3
         WHERE id = $4`,
        [response.scope.description, response.scope.summary, response.scope.estimated_cost, featureId]
      );
    }

    res.json(response);
  } catch (err) {
    console.error('Error processing feature message:', (err as Error).message);
    if ((err as Error).name === 'AssistantNotConfiguredError') {
      respondAssistantNotConfigured(res);
    } else if ((err as Error).name === 'CircuitBreakerOpenError') {
      res.status(503).json({ error: (err as Error).message });
    } else {
      res.status(500).json({ error: 'Failed to process message. Please try again.' });
    }
  }
});

router.post('/features/:featureId/approve', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  const { featureId } = req.params;
  const admin = isAdmin(req);

  try {
    const query = admin
      ? "SELECT * FROM feature_requests WHERE id = $1 AND status = 'scoped'"
      : "SELECT * FROM feature_requests WHERE id = $1 AND customer_id = $2 AND status = 'scoped'";
    const params = admin ? [featureId] : [featureId, req.session.customerId];
    const featureResult = await pool.query(query, params);

    if (featureResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    await pool.query(
      "UPDATE feature_requests SET status = 'payment_pending' WHERE id = $1",
      [featureId]
    );

    res.redirect(`/features/${featureId}/pay`);
  } catch (err) {
    console.error('Error approving feature:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to approve feature request.' });
  }
});

router.get('/features/:featureId/pay', requireAuth, async (req: Request, res: Response) => {
  const { featureId } = req.params;
  const admin = isAdmin(req);

  try {
    const query = admin
      ? "SELECT fr.*, b.app_name FROM feature_requests fr JOIN builds b ON fr.build_id = b.id WHERE fr.id = $1 AND fr.status = 'payment_pending'"
      : "SELECT fr.*, b.app_name FROM feature_requests fr JOIN builds b ON fr.build_id = b.id WHERE fr.id = $1 AND fr.customer_id = $2 AND fr.status = 'payment_pending'";
    const params = admin ? [featureId] : [featureId, req.session.customerId];
    const featureResult = await pool.query(query, params);

    if (featureResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const feature = featureResult.rows[0] as FeatureRequest & { app_name: string | null };
    const costDisplay = feature.estimated_cost === 4900 ? '$49' :
      feature.estimated_cost === 9900 ? '$99' : '$199';
    const sizeLabel = feature.estimated_cost === 4900 ? 'Small' :
      feature.estimated_cost === 9900 ? 'Medium' : 'Large';

    const csrfToken = generateCsrfToken(req);

    res.render('feature-payment', {
      title: 'Feature Payment',
      feature,
      appName: feature.app_name || 'Your Website',
      costDisplay,
      sizeLabel,
      csrfToken,
      // Shows the "skip payment" control. The POST re-checks the same rule.
      devMode: paymentBypassAllowed(admin),
    });
  } catch (err) {
    console.error('Error loading feature payment:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load payment page.' });
  }
});

router.post('/features/:featureId/bypass-dev', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  const { featureId } = req.params;
  const admin = isAdmin(req);

  // STRIPE_BYPASS is an escape hatch for the admin and for local testing. In
  // production it must not turn into "every customer may skip payment", so
  // there it is honored for the admin account only.
  if (!paymentBypassAllowed(admin)) {
    res.status(403).send('Forbidden');
    return;
  }

  try {
    const query = admin
      ? `SELECT fr.id, fr.status, fr.build_id, b.app_name, c.email, c.name
         FROM feature_requests fr
         JOIN builds b ON fr.build_id = b.id
         JOIN customers c ON fr.customer_id = c.id
         WHERE fr.id = $1`
      : `SELECT fr.id, fr.status, fr.build_id, b.app_name, c.email, c.name
         FROM feature_requests fr
         JOIN builds b ON fr.build_id = b.id
         JOIN customers c ON fr.customer_id = c.id
         WHERE fr.id = $1 AND fr.customer_id = $2`;
    const params = admin ? [featureId] : [featureId, req.session.customerId];
    const featureResult = await pool.query(query, params);

    if (featureResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const row = featureResult.rows[0] as {
      id: string;
      status: string;
      build_id: string;
      app_name: string | null;
      email: string;
      name: string;
    };

    if (row.status !== 'payment_pending' && row.status !== 'scoped') {
      res.redirect(`/features/${featureId}`);
      return;
    }

    await pool.query(
      "UPDATE feature_requests SET status = 'queued' WHERE id = $1",
      [featureId]
    );

    sendBuildQueuedEmail(
      row.email,
      row.name,
      `Feature for ${row.app_name || row.build_id}`
    ).catch(err =>
      console.error('[features] Queued email failed:', (err as Error).message)
    );

    // Redirect to build status page so the customer sees the build progress
    res.redirect(`/builds/${row.build_id}`);
  } catch (err) {
    console.error('Error in feature dev bypass:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Bypass failed.' });
  }
});

export default router;
