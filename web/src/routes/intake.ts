import { respondAssistantNotConfigured } from '../utils/assistant';
import { Router, Request, Response, NextFunction } from 'express';
import multer from 'multer';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import { boundedRateLimit, customerOrAddressKey } from '../middleware/companion-rate-limit';
import { requireAuth, generateCsrfToken, validateCsrfToken, verifyHmacToken } from '../middleware/auth';
import { processIntakeMessage, generateInitialGreeting } from '../services/claude';
import { INTAKE_HARD_TURN_CAP } from '../services/llm-budget';
import { customerLlmDailyLimit } from '../middleware/llm-rate-limit';
import { processLogo, InvalidLogoError, LogoBusyError } from '../services/logo';
import { IntakeMessage, AppSpec } from '../types';
import { checkSpecSimplicity, checkProhibitedContent } from '../services/spec-validation';
import { resumeIfAbandoned } from '../services/abandon-check';

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 2 * 1024 * 1024 }, // 2MB max
  fileFilter: (_req, file, cb) => {
    const allowed = ['image/png', 'image/jpeg', 'image/svg+xml', 'image/webp'];
    if (allowed.includes(file.mimetype)) {
      cb(null, true);
    } else {
      cb(new Error('Only PNG, JPEG, SVG, and WebP images are allowed.'));
    }
  },
});

const router = forwardAsyncErrors(Router());

// multer reports an oversized or filtered file by passing an error to next(),
// which would land in the app-level HTML 500 page. The logo client expects
// JSON, so translate upload errors here.
function logoUpload(req: Request, res: Response, next: NextFunction): void {
  upload.single('logo')(req, res, (err?: unknown) => {
    if (!err) {
      next();
      return;
    }
    if (err instanceof multer.MulterError && err.code === 'LIMIT_FILE_SIZE') {
      res.status(413).json({ error: 'That image is larger than 2 MB. Please choose a smaller file.' });
      return;
    }
    res.status(400).json({ error: (err as Error).message || 'Upload failed.' });
  });
}

const intakeRateLimit = boundedRateLimit({
  windowMs: 60 * 1000,
  max: 30,
  message: { error: 'Too many messages. Please wait a moment before sending again.' },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: customerOrAddressKey,
});

// Logo uploads get their own, smaller budget: every accepted upload costs an
// image decode or an SVG rebuild. The limiter runs before the multipart
// parser, so a refused request is not read into memory first.
export const LOGO_UPLOAD_WINDOW_MS = 15 * 60 * 1000;
export const LOGO_UPLOAD_MAX = 20;
const logoUploadRateLimit = boundedRateLimit({
  windowMs: LOGO_UPLOAD_WINDOW_MS,
  max: LOGO_UPLOAD_MAX,
  message: { error: 'Too many logo uploads. Please wait a few minutes and try again.' },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: customerOrAddressKey,
});

// When CAS_HARBOR_ENABLED=true, /intake is the legacy multi-step intake and
// Haven at /lighthouse/table is the canonical conversational surface (shared
// with anon /try; same chat for everyone). Logged-in users are redirected
// there, where their session.customerId is detected for keeper mode and the
// "Build my website" handoff button. The redirect sits in front of the daily
// limiter so that a redirect, which costs nothing, is never counted or refused.
function harborIntakeRedirect(req: Request, res: Response, next: NextFunction): void {
  if (process.env.CAS_HARBOR_ENABLED === 'true') {
    res.redirect(302, '/lighthouse/table');
    return;
  }
  next();
}

// Every visit here creates a builds row and asks the model for a greeting, so
// it draws on the customer's daily budget like a chat message does.
router.get('/', requireAuth, harborIntakeRedirect, customerLlmDailyLimit, async (req: Request, res: Response) => {
  try {
    const result = await pool.query(
      `INSERT INTO builds (customer_id, tier, status)
       VALUES ($1, $2, 'intake')
       RETURNING id`,
      [req.session.customerId, 'build']
    );

    const buildId = (result.rows[0] as { id: string }).id;

    const greeting = await generateInitialGreeting();

    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'assistant', greeting]
    );

    res.redirect(`/intake/${buildId}`);
  } catch (err) {
    console.error('Error starting intake:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to start intake. Please try again.' });
  }
});

// Seeded intake kickoff for logged-in users: mirrors POST /try/start so the
// homepage hero textarea works for authenticated visitors too. Creates the
// intake build, greets, processes the first turn through Claude, and returns
// { buildId } for the client to navigate to /intake/:buildId with the
// conversation already flowing.
router.post('/start', requireAuth, intakeRateLimit, customerLlmDailyLimit, async (req: Request, res: Response) => {
  const csrfToken = req.headers['x-csrf-token'] as string;
  if (!csrfToken || !verifyHmacToken(csrfToken, req)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }

  const rawSeed = typeof req.body?.message === 'string' ? req.body.message : '';
  const seedMessage = rawSeed.trim().slice(0, 2000);
  if (!seedMessage) {
    res.status(400).json({ error: 'Message is required.' });
    return;
  }

  try {
    const greeting = await generateInitialGreeting();
    const response = await processIntakeMessage(seedMessage, [], 0);

    const result = await pool.query(
      `INSERT INTO builds (customer_id, tier, status)
       VALUES ($1, $2, 'intake')
       RETURNING id`,
      [req.session.customerId, 'build']
    );
    const buildId = (result.rows[0] as { id: string }).id;

    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'assistant', greeting]
    );
    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'user', seedMessage]
    );
    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'assistant', response.reply]
    );

    res.json({ buildId });
  } catch (err) {
    console.error('Error seeding intake:', (err as Error).message);
    res.status(500).json({ error: 'Failed to start intake. Please try again.' });
  }
});

router.get('/:buildId', requireAuth, async (req: Request, res: Response) => {
  // Harbor flag on: any /intake/<id> bookmark redirects to the harbor
  // companion. Build-status-specific paths (review, queued, deploying)
  // live on /builds/:id and aren't affected.
  if (process.env.CAS_HARBOR_ENABLED === 'true') {
    res.redirect(302, '/lighthouse/table');
    return;
  }
  const { buildId } = req.params;

  try {
    // Columns are listed on purpose: SELECT * would also pull build_artifact
    // (the source tarball, up to tens of megabytes) and railway_database_url
    // (the customer database's address) into the web process for a page that
    // needs neither
    // (see GET /builds/:buildId in routes/builds.ts).
    const buildResult = await pool.query(
      'SELECT id, customer_id, status, tier, spec FROM builds WHERE id = $1 AND customer_id = $2',
      [buildId, req.session.customerId]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = buildResult.rows[0] as {
      id: string;
      customer_id: string | null;
      status: string;
      tier: string;
      spec: { app_name?: string; preview?: unknown } | null;
    };

    if (build.status === 'abandoned') {
      await resumeIfAbandoned(buildId);
    } else if (build.status !== 'intake') {
      res.redirect(`/builds/${buildId}`);
      return;
    }

    const messagesResult = await pool.query(
      'SELECT * FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
      [buildId]
    );

    const messages = messagesResult.rows as IntakeMessage[];
    const csrfToken = generateCsrfToken(req);
    const turnCount = messages.filter((m) => m.role === 'user').length;
    const isAnonymous = !build.customer_id;

    // Anonymous /try drafts store `{preview: AnonSpecPreview}` in builds.spec.
    // That's not a final spec, only a live sketch to prefill the panel while
    // the authenticated customer continues the conversation. A real,
    // confirmable spec has top-level fields like app_name.
    const specObj = build.spec;
    const specComplete = !!(specObj && typeof (specObj as { app_name?: unknown }).app_name === 'string');
    const specPreview = !specComplete && specObj && typeof specObj === 'object' && 'preview' in specObj
      ? (specObj as { preview?: unknown }).preview ?? null
      : null;

    res.render('intake', {
      title: 'Describe Your Website',
      build,
      messages,
      csrfToken,
      specComplete,
      specPreview,
      isAnonymous,
      turnCount,
    });
  } catch (err) {
    console.error('Error loading intake:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load intake.' });
  }
});

router.post('/message', requireAuth, intakeRateLimit, customerLlmDailyLimit, async (req: Request, res: Response) => {
  const { buildId, message } = req.body as { buildId?: unknown; message?: unknown };
  const csrfToken = req.headers['x-csrf-token'] as string;

  if (!csrfToken || !verifyHmacToken(csrfToken, req)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }

  // Body fields are attacker-controlled: a JSON number or an array
  // (message[]=x) has no .trim() and used to throw outside the try block.
  if (typeof buildId !== 'string' || !buildId || typeof message !== 'string' || message.trim().length === 0) {
    res.status(400).json({ error: 'buildId and message are required.' });
    return;
  }

  const sanitizedMessage = message.trim().slice(0, 2000);

  try {
    // Only the status is read (see GET /:buildId above for why no SELECT *).
    const buildResult = await pool.query(
      'SELECT status FROM builds WHERE id = $1 AND customer_id = $2 AND status = ANY($3)',
      [buildId, req.session.customerId, ['intake', 'abandoned']]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not in intake status.' });
      return;
    }

    if ((buildResult.rows[0] as { status: string }).status === 'abandoned') {
      await resumeIfAbandoned(buildId);
    }

    const historyResult = await pool.query(
      'SELECT role, content, created_at FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
      [buildId]
    );

    const history = historyResult.rows as IntakeMessage[];

    // Defensive: remove trailing user messages with no assistant reply
    // (left over from previous failed API calls before this fix)
    while (history.length > 0 && history[history.length - 1].role === 'user') {
      history.pop();
    }

    const turnCount = history.filter((m) => m.role === 'user').length;

    // Every turn resends the whole conversation, so a conversation must end
    // somewhere. Checked before the model is called.
    if (turnCount >= INTAKE_HARD_TURN_CAP) {
      res.status(409).json({
        error:
          'This conversation has reached its maximum length. Please start a new one from your dashboard, or write to support@craftandship.io.',
      });
      return;
    }

    // Call Claude BEFORE persisting the user message. If the API call fails,
    // the message is not saved, so the conversation stays clean and retryable.
    const response = await processIntakeMessage(sanitizedMessage, history, turnCount);

    // Persist both user and assistant messages together after success
    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'user', sanitizedMessage]
    );
    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'assistant', response.reply]
    );

    if (response.specComplete && response.spec) {
      await pool.query(
        'UPDATE builds SET spec = $1, app_name = $2 WHERE id = $3',
        [JSON.stringify(response.spec), response.spec.app_name, buildId]
      );

      // Surface optional simplicity warnings; tier-suggestion was retired
      // 2026-05-01 with the tier system kill. Harbor sells one product.
      const simplicityWarnings = checkSpecSimplicity(response.spec);

      res.json({
        ...response,
        simplicityWarnings,
      });
      return;
    }

    res.json(response);
  } catch (err) {
    console.error('Error processing intake message:', (err as Error).message);
    if ((err as Error).name === 'AssistantNotConfiguredError') {
      respondAssistantNotConfigured(res);
    } else if ((err as Error).name === 'CircuitBreakerOpenError') {
      res.status(503).json({ error: (err as Error).message });
    } else {
      res.status(500).json({ error: 'Failed to process message. Please try again.' });
    }
  }
});

// Logo upload: process image, extract colors, update spec design field
router.post('/:buildId/logo', requireAuth, logoUploadRateLimit, logoUpload, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const csrfToken = req.headers['x-csrf-token'] as string;

  if (!csrfToken || !verifyHmacToken(csrfToken, req)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }

  if (!req.file) {
    res.status(400).json({ error: 'No file uploaded.' });
    return;
  }

  try {
    const buildResult = await pool.query(
      'SELECT id, spec, status FROM builds WHERE id = $1 AND customer_id = $2 AND status = ANY($3)',
      [buildId, req.session.customerId, ['intake', 'abandoned']]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not in intake status.' });
      return;
    }

    if ((buildResult.rows[0] as { status: string }).status === 'abandoned') {
      await resumeIfAbandoned(buildId);
    }

    const build = buildResult.rows[0] as { id: string; spec: Record<string, unknown> | null };
    const logoResult = await processLogo(req.file.buffer, req.file.mimetype);

    // Update spec with logo and extracted colors
    const currentSpec = build.spec || {};
    const currentDesign = (currentSpec.design as Record<string, unknown>) || {};

    const updatedDesign = {
      ...currentDesign,
      primary_color: logoResult.colors.primary_color,
      secondary_color: logoResult.colors.secondary_color,
      accent_color: logoResult.colors.accent_color,
      logo_data: `data:${logoResult.mimetype};base64,${logoResult.cleanedBase64}`,
    };

    const updatedSpec = { ...currentSpec, design: updatedDesign };

    // The filename is stored for display only (nothing is written to disk), but
    // it is client-controlled: keep the last path segment, drop control
    // characters, and fit logo_filename VARCHAR(255).
    const safeFilename = (req.file.originalname.split(/[\\/]/).pop() || 'logo')
      .replace(/[\u0000-\u001f\u007f]/g, '')
      .slice(0, 255) || 'logo';

    await pool.query(
      'UPDATE builds SET spec = $1, logo_filename = $2, logo_mimetype = $3 WHERE id = $4',
      [JSON.stringify(updatedSpec), safeFilename, logoResult.mimetype, buildId]
    );

    res.json({
      success: true,
      colors: logoResult.colors,
      logo_preview: `data:${logoResult.mimetype};base64,${logoResult.cleanedBase64}`,
      width: logoResult.width,
      height: logoResult.height,
    });
  } catch (err) {
    if (err instanceof InvalidLogoError) {
      res.status(400).json({ error: err.message });
      return;
    }
    if (err instanceof LogoBusyError) {
      // Too many SVG rebuilds are already in flight; nothing was started.
      res.set('Retry-After', '5');
      res.status(503).json({ error: 'The server is busy with other uploads. Please try again in a moment.' });
      return;
    }
    console.error('Error processing logo:', (err as Error).message);
    res.status(500).json({ error: 'Failed to process logo. Please try a different image.' });
  }
});

// `POST /:buildId/confirm` (legacy tier-confirm + redirect-to-/payment) was
// removed 2026-05-01 alongside the /payment route. With Harbor, customers go
// through /lighthouse/table; this legacy intake flow has no destination.

export default router;
