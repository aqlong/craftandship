import { Router, Request, Response } from 'express';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import { requireAuth, validateCsrfToken } from '../middleware/auth';
import { displayName } from '../utils/format';

const router = forwardAsyncErrors(Router());

interface GalleryRow {
  id: string;
  app_name: string | null;
  tier: string;
  live_url: string | null;
  gallery_description: string | null;
  gallery_category: string | null;
  gallery_featured: boolean;
  gallery_display_name: string | null;
  created_at: Date;
  // First 200 characters of spec.description, computed in SQL.
  spec_description: string | null;
  // One entry per stored screenshot that has image data, computed in SQL.
  // `idx` is the position inside builds.screenshots (what
  // /gallery/screenshots/:id/:idx serves); `v` is the first 8 hex characters
  // of md5(image data), used as a cache-bust token.
  screenshot_meta: Array<{ page: string | null; idx: number; v: string }> | null;
}

const GALLERY_LIMIT = 50;

function boundedText(value: unknown, maxLength: number): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim().slice(0, maxLength);
  return trimmed || null;
}

// GET /gallery: public gallery of live websites
router.get('/gallery', async (req: Request, res: Response) => {
  try {
    const category = req.query.category as string | undefined;

    // The screenshots column holds base64 PNGs (hundreds of KB each) and spec
    // can embed an uploaded logo, so this unauthenticated, uncached page must
    // not ship either to Node just to count entries and hash bytes. Postgres
    // reduces them to a few small fields per row instead.
    let query = `
      SELECT b.id, b.app_name, b.tier, b.live_url, b.gallery_description,
             b.gallery_category, b.gallery_featured, b.gallery_display_name,
             b.created_at,
             LEFT(b.spec->>'description', 200) AS spec_description,
             (SELECT COALESCE(
                       jsonb_agg(
                         jsonb_build_object(
                           'page', s.elem->>'page',
                           'idx', s.ord - 1,
                           'v', substr(md5(s.elem->>'data'), 1, 8)
                         ) ORDER BY s.ord
                       ),
                       '[]'::jsonb)
                FROM jsonb_array_elements(
                       CASE WHEN jsonb_typeof(b.screenshots) = 'array' THEN b.screenshots ELSE '[]'::jsonb END
                     ) WITH ORDINALITY AS s(elem, ord)
               WHERE COALESCE(s.elem->>'data', '') <> ''
             ) AS screenshot_meta
      FROM builds b
      WHERE b.status = 'live' AND b.gallery_opted_in = TRUE
    `;
    const params: string[] = [];

    if (category) {
      params.push(category);
      query += ` AND b.gallery_category = $1`;
    }

    query += ` ORDER BY b.gallery_featured DESC, b.created_at DESC LIMIT ${GALLERY_LIMIT}`;

    const result = await pool.query<GalleryRow>(query, params);

    const apps = result.rows.map((row) => {
      const description = row.gallery_description || row.spec_description || '';

      // Screenshot URLs for the preview carousel
      const screenshotUrls = (row.screenshot_meta ?? []).map((shot) => ({
        url: `/gallery/screenshots/${row.id}/${shot.idx}?v=${shot.v}`,
        page: shot.page || `Page ${shot.idx + 1}`,
        isLogin: shot.page === '/login',
        hasPage: !!shot.page,
      }));
      // Thumbnail: the first named page that is not the login screen, else the first shot.
      const thumbnail = screenshotUrls.find((shot) => shot.hasPage && !shot.isLogin) ?? screenshotUrls[0];

      return {
        id: row.id,
        app_name: row.app_name,
        // gallery_display_name overrides the derived slug→title: lets the
        // operator preserve a brand name like "Sample Studio Website, with CMS"
        // without mutating the canonical app_name slug.
        display_name: row.gallery_display_name || displayName(row.app_name ?? ''),
        tier: row.tier,
        live_url: row.live_url,
        description,
        category: row.gallery_category,
        featured: row.gallery_featured,
        created_at: row.created_at,
        screenshot_url: thumbnail?.url ?? null,
        screenshot_urls: screenshotUrls.map(({ url, page }) => ({ url, page })),
      };
    });

    const categories: string[] = [
      ...new Set(
        result.rows
          .map((row) => row.gallery_category)
          .filter((c): c is string => !!c)
      )
    ].sort();

    res.render('gallery', {
      title: 'Website Gallery',
      metaDescription: 'Real practitioner websites, built from a library of tested building blocks, and each needs a recorded review approval before it goes live.',
      apps,
      categories,
      currentCategory: category || null
    });
  } catch (err) {
    console.error('gallery: failed to load gallery', err);
    res.status(500).render('500', { title: 'Server Error' });
  }
});

// GET /gallery/screenshots/:buildId/:index, public screenshot for gallery-opted-in builds only
router.get('/gallery/screenshots/:buildId/:index', async (req: Request, res: Response) => {
  const { buildId, index } = req.params;
  const idx = parseInt(index, 10);

  try {
    // Only serve screenshots for gallery-opted-in live builds
    const result = await pool.query(
      'SELECT screenshots FROM builds WHERE id = $1 AND status = \'live\' AND gallery_opted_in = TRUE',
      [buildId]
    );

    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Not found.' });
      return;
    }

    const screenshots = result.rows[0].screenshots || [];
    if (isNaN(idx) || idx < 0 || idx >= screenshots.length) {
      res.status(404).json({ error: 'Screenshot not found.' });
      return;
    }

    const entry = screenshots[idx];
    if (!entry.data) {
      res.status(404).json({ error: 'Screenshot data missing.' });
      return;
    }

    const buffer = Buffer.from(entry.data, 'base64');
    res.setHeader('Content-Type', 'image/png');
    res.setHeader('Cache-Control', 'public, max-age=86400');
    res.setHeader('Content-Length', buffer.length.toString());
    res.end(buffer);
  } catch (err) {
    console.error('Error serving gallery screenshot:', (err as Error).message);
    res.status(500).json({ error: 'Failed to load screenshot.' });
  }
});

// POST /builds/:buildId/gallery-optin, toggle gallery opt-in for a build
router.post('/builds/:buildId/gallery-optin', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const customerId = req.session.customerId;
    const optedIn = req.body.gallery_opted_in === 'true';
    // Free-text fields are bounded to their column sizes (VARCHAR(500) and
    // VARCHAR(100)); an oversized or non-string value used to surface as a 500
    // from Postgres after the form was submitted.
    const description = boundedText(req.body.gallery_description, 500);
    const category = boundedText(req.body.gallery_category, 100);

    const result = await pool.query(
      `UPDATE builds
       SET gallery_opted_in = $1,
           gallery_description = COALESCE($2, gallery_description),
           gallery_category = COALESCE($3, gallery_category)
       WHERE id = $4 AND customer_id = $5 AND status IN ('live', 'preview')
       RETURNING id`,
      [optedIn, description, category, buildId, customerId]
    );

    if (result.rows.length === 0) {
      return res.status(404).render('404', { title: 'Not Found' });
    }

    res.redirect('/dashboard');
  } catch (err) {
    console.error('gallery: failed to update gallery opt-in', err);
    res.status(500).render('500', { title: 'Server Error' });
  }
});

export default router;
