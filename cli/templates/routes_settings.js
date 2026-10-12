const express = require('express');
const router = express.Router();
const pool = require('../db');
const { requireAuth, validateCsrfToken } = require('../middleware/auth');

// Admin guard
function requireAdmin(req, res, next) {
  if (!req.session.userId) return res.redirect('/login');
  if (req.session.userRole !== 'admin' && req.session.userId !== 1) {
    return res.status(403).render('403', { title: 'Forbidden' });
  }
  next();
}

// ADMIN_SETTINGS_SUPERSET (MW v12 fix): keys recognized by the settings UI + save
// handler. Grouped by section. Apps that only seed some keys still work; unseeded
// keys come back empty from SELECT and empty form posts INSERT blank values. The
// UI only renders an input for a key if it was seeded (see view template). The
// superset is what the founder can configure across ALL generated CAS apps.
const SETTING_FIELDS = [
  // Brand + site identity
  'site_name', 'site_url', 'site_description',
  // Hero (home page top)
  'hero_tagline', 'hero_description',
  // About page
  'about_title', 'about_body',
  // Contact block (public /contact page)
  'contact_email', 'contact_phone', 'contact_address',
  // Legacy / generic contact (older apps; still accepted)
  'business_name', 'phone', 'support_email', 'address',
  // Uploadable images (stored as /uploads/<id> URLs from upload route)
  'nav_logo_url', 'hero_image_url', 'about_photo_url',
  // Social
  'instagram_url', 'facebook_url', 'youtube_url',
  'tiktok_url', 'twitter_url', 'linkedin_url',
];

// GET /admin/settings
router.get('/admin/settings', requireAdmin, async (req, res) => {
  try {
    const result = await pool.query('SELECT key, value FROM site_settings');
    const settings = {};
    result.rows.forEach(r => { settings[r.key] = r.value; });
    const flash = req.session.flash || '';
    delete req.session.flash;
    res.render('admin/settings', {
      title: 'Site Settings',
      settings,
      flash,
      csrfToken: res.locals.csrfToken || ''
    });
  } catch (err) {
    console.error('Settings page error:', err);
    res.status(500).render('500', { title: 'Error' });
  }
});

// POST /admin/settings
router.post('/admin/settings', requireAdmin, validateCsrfToken, async (req, res) => {
  try {
    for (const field of SETTING_FIELDS) {
      const value = (req.body[field] || '').trim();
      await pool.query(
        `INSERT INTO site_settings (key, value, updated_at) VALUES ($1, $2, NOW())
         ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()`,
        [field, value]
      );
    }
    // Clear settings cache
    if (global._settingsCache) {
      global._settingsCache = null;
      global._settingsCacheTime = 0;
    }
    req.session.flash = 'Settings saved successfully';
    req.session.save(() => res.redirect('/admin/settings'));
  } catch (err) {
    console.error('Settings save error:', err);
    req.session.flash = 'Error saving settings';
    req.session.save(() => res.redirect('/admin/settings'));
  }
});

module.exports = router;
