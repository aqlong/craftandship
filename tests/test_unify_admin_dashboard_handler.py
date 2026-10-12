"""Tests for _unify_admin_dashboard_handler.

GET /admin must not render views/dashboard.ejs directly because
dashboard.ejs references `recentInquiries` (passed only by the
canonical /dashboard handler in routes/dashboard.js). The processor
rewrites res.render('dashboard', {...}) calls in routes/admin.js
to res.redirect('/dashboard') so the single canonical handler runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


# Realistic routes/admin.js shape.
ADMIN_JS_WITH_DASHBOARD_RENDER = """\
const express = require('express');
const router = express.Router();
const db = require('../db');
const { requireAuth, requireRole } = require('../middleware/auth');

router.get('/', requireAuth, async (req, res) => {
  try {
    const testimonialsCount = (await db.query('SELECT COUNT(*) FROM testimonials')).rows[0].count;
    const stats = { testimonials: testimonialsCount };
    res.render('dashboard', { title: 'Admin Dashboard', stats, testimonials: testimonialsCount });
  } catch (err) {
    console.error(err);
    res.status(500).render('500', { title: 'Error' });
  }
});

module.exports = router;
"""


ADMIN_JS_ALREADY_REDIRECTS = """\
const express = require('express');
const router = express.Router();

router.get('/', (req, res) => {
  res.redirect('/dashboard');
});

module.exports = router;
"""


ADMIN_JS_NO_DASHBOARD = """\
const express = require('express');
const router = express.Router();

router.get('/users', async (req, res) => {
  res.render('admin-users', { users: [] });
});

module.exports = router;
"""


def test_f4_dashboard_render_rewritten_to_redirect():
    files = [("routes/admin.js", ADMIN_JS_WITH_DASHBOARD_RENDER)]
    out = dict(build._unify_admin_dashboard_handler(files))
    got = out["routes/admin.js"]
    # render call replaced with redirect.
    assert "res.redirect('/dashboard');" in got
    # Original render call is gone.
    assert "res.render('dashboard'" not in got


def test_f4_already_redirecting_unchanged():
    """routes/admin.js that already redirects must not be modified."""
    files = [("routes/admin.js", ADMIN_JS_ALREADY_REDIRECTS)]
    out = dict(build._unify_admin_dashboard_handler(files))
    # processor returns original `files` object unchanged when nothing to do
    assert out["routes/admin.js"] == ADMIN_JS_ALREADY_REDIRECTS


def test_f4_no_dashboard_render_unchanged():
    """If admin.js doesn't render dashboard, processor is a no-op."""
    files = [("routes/admin.js", ADMIN_JS_NO_DASHBOARD)]
    out = dict(build._unify_admin_dashboard_handler(files))
    assert out["routes/admin.js"] == ADMIN_JS_NO_DASHBOARD


def test_f4_no_admin_js_no_op():
    """If routes/admin.js is missing entirely, no crash."""
    files = [("server.js", "// nothing")]
    out = dict(build._unify_admin_dashboard_handler(files))
    # Unchanged.
    assert out == {"server.js": "// nothing"}


def test_f4_idempotent_reapply():
    """Running twice produces the same output as running once."""
    files = [("routes/admin.js", ADMIN_JS_WITH_DASHBOARD_RENDER)]
    once = dict(build._unify_admin_dashboard_handler(files))
    twice = dict(build._unify_admin_dashboard_handler(list(once.items())))
    assert once["routes/admin.js"] == twice["routes/admin.js"]
    # No stray `render('dashboard')` after second pass either.
    assert "res.render('dashboard'" not in twice["routes/admin.js"]


def test_f4_does_not_touch_dashboard_js():
    """routes/dashboard.js is the canonical handler, untouched."""
    dashboard_js = """\
router.get('/', async (req, res) => {
  const stats = { testimonials: 0 };
  const recentInquiries = [];
  res.render('dashboard', { stats, recentInquiries });
});
"""
    files = [
        ("routes/admin.js", ADMIN_JS_WITH_DASHBOARD_RENDER),
        ("routes/dashboard.js", dashboard_js),
    ]
    out = dict(build._unify_admin_dashboard_handler(files))
    # admin.js rewritten.
    assert "res.redirect('/dashboard');" in out["routes/admin.js"]
    # dashboard.js unchanged: this is the canonical handler.
    assert out["routes/dashboard.js"] == dashboard_js
    assert "res.render('dashboard'" in out["routes/dashboard.js"]


def test_f4_multiline_render_call_rewritten():
    """Some Claude emissions break the render call across lines.
    The regex must handle reasonable whitespace variation."""
    admin_multiline = """\
router.get('/', async (req, res) => {
  res.render('dashboard', {
    title: 'Admin Dashboard',
    stats: {}
  });
});
"""
    files = [("routes/admin.js", admin_multiline)]
    out = dict(build._unify_admin_dashboard_handler(files))
    # After rewrite, the call should include a redirect.
    assert "res.redirect('/dashboard');" in out["routes/admin.js"]
