import { Router, Request, Response } from 'express';
import { requireAuth, verifyHmacToken, isAdminSession } from '../middleware/auth';
import { boundedRateLimit, customerOrAddressKey } from '../middleware/companion-rate-limit';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import { renderErrorPage } from '../utils/error-page';

const router = forwardAsyncErrors(Router());

const VALID_CATEGORIES = ['bug', 'suggestion', 'question', 'other'];
const MAX_MESSAGE_LENGTH = 2000;
const PAGE_SIZE = 50;

const feedbackRateLimit = boundedRateLimit({
  windowMs: 60 * 1000,
  max: 5,
  message: { error: 'Too many feedback submissions. Please wait a moment.' },
  skip: () => process.env.NODE_ENV === 'test',
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: customerOrAddressKey,
});

// POST /feedback - submit beta feedback (JSON API, uses header-based CSRF)
router.post('/feedback', requireAuth, feedbackRateLimit, async (req: Request, res: Response): Promise<void> => {
  const csrfToken = req.headers['x-csrf-token'] as string;
  if (!csrfToken || !verifyHmacToken(csrfToken)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }
  const { category, message, page_url } = req.body as {
    category?: unknown;
    message?: unknown;
    page_url?: unknown;
  };

  if (typeof category !== 'string' || !VALID_CATEGORIES.includes(category)) {
    res.status(400).json({ error: 'Invalid category. Must be one of: bug, suggestion, question, other.' });
    return;
  }

  // message and page_url come straight from the JSON body; an array or object
  // has no .trim()/.slice() and used to throw outside the try block.
  const trimmedMessage = (typeof message === 'string' ? message : '').trim();
  if (!trimmedMessage) {
    res.status(400).json({ error: 'Message is required.' });
    return;
  }
  if (trimmedMessage.length > MAX_MESSAGE_LENGTH) {
    res.status(400).json({ error: `Message must be under ${MAX_MESSAGE_LENGTH} characters.` });
    return;
  }

  try {
    await pool.query(
      `INSERT INTO beta_feedback (customer_id, category, message, page_url, user_agent)
       VALUES ($1, $2, $3, $4, $5)`,
      [
        req.session.customerId,
        category,
        trimmedMessage,
        (typeof page_url === 'string' ? page_url : '').slice(0, 500),
        req.headers['user-agent'] || '',
      ]
    );

    // JSON.stringify keeps a multi-line message from forging extra log lines.
    console.log(`[feedback] ${category} from ${req.session.customerEmail}: ${JSON.stringify(trimmedMessage.slice(0, 100))}`);
    res.json({ success: true });
  } catch (err) {
    console.error('[feedback] insert failed:', (err as Error).message);
    res.status(500).json({ error: 'Failed to save feedback.' });
  }
});

// GET /admin/feedback - admin view of all feedback
function requireAdminMiddleware(req: Request, res: Response, next: () => void): void {
  if (!req.session.customerId) {
    res.redirect(`/login?returnTo=${encodeURIComponent(req.originalUrl)}`);
    return;
  }
  if (!isAdminSession(req)) {
    renderErrorPage(res, 403, { title: 'Forbidden', error: 'Admin access required.' });
    return;
  }
  next();
}

router.get('/admin/feedback', requireAdminMiddleware, async (req: Request, res: Response): Promise<void> => {
  const page = Math.max(1, parseInt(req.query.page as string, 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;
  const categoryFilter = VALID_CATEGORIES.includes(req.query.category as string) ? req.query.category as string : '';
  const qFilter = ((req.query.q as string) ?? '').trim();

  try {
    const conditions: string[] = [];
    const params: string[] = [];
    let paramIdx = 1;

    if (categoryFilter) {
      conditions.push(`bf.category = $${paramIdx}`);
      params.push(categoryFilter);
      paramIdx++;
    }

    if (qFilter) {
      conditions.push(`(bf.message ILIKE $${paramIdx} OR c.email ILIKE $${paramIdx} OR c.name ILIKE $${paramIdx})`);
      params.push(`%${qFilter}%`);
      paramIdx++;
    }

    const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const countQuery = `SELECT COUNT(*) FROM beta_feedback bf
                        LEFT JOIN customers c ON bf.customer_id = c.id
                        ${whereClause}`;
    const dataQuery = `SELECT bf.*, c.email as customer_email, c.name as customer_name
                       FROM beta_feedback bf
                       LEFT JOIN customers c ON bf.customer_id = c.id
                       ${whereClause}
                       ORDER BY bf.created_at DESC LIMIT ${PAGE_SIZE} OFFSET ${offset}`;
    const insightsQuery = `SELECT category, COUNT(*)::int as count FROM beta_feedback GROUP BY category ORDER BY count DESC`;

    const [countResult, dataResult, insightsResult] = await Promise.all([
      pool.query(countQuery, params),
      pool.query(dataQuery, params),
      pool.query(insightsQuery),
    ]);

    const total = parseInt(countResult.rows[0].count, 10);
    const totalPages = Math.ceil(total / PAGE_SIZE);
    const categoryCounts = insightsResult.rows as Array<{ category: string; count: number }>;

    res.render('admin-feedback', { activeTab: 'feedback',
      title: 'Beta Feedback',
      feedback: dataResult.rows,
      page,
      totalPages,
      total,
      categoryFilter,
      qFilter,
      categories: VALID_CATEGORIES,
      categoryCounts,
    });
  } catch (err) {
    console.error('[admin/feedback] query failed:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load feedback.' });
  }
});

export default router;
