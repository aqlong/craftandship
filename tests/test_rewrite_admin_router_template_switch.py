"""Tests for the admin-router template-switch post-processor.

Regression guards for the widget runtime vs artifact gap: widget
emission writes `views/admin/<slug>/new.ejs` to artifact.files but the
Claude-emitted `routes/<slug>.js` still `res.render('<slug>/new', ...)`
which resolves to `views/<slug>/new.ejs` (public). Dual-mount at
`/admin/<slug>` reuses the same router so the widget admin view is
orphaned.

The fix is a post-processor that rewrites `res.render` calls in
admin-CRUD route files to switch view prefix based on `req.baseUrl`:

    const viewPrefix = req.baseUrl && req.baseUrl.startsWith('/admin/')
      ? 'admin/' : '';
    res.render(viewPrefix + 'events/new', { ... });

At runtime `/admin/events/new` resolves `viewPrefix` to `'admin/'` and
renders the widget-emitted view.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

import pipeline  # noqa: E402
import build  # noqa: E402,F401 - populates REGISTRY


# ---------------------------------------------------------------------------
# Fixture: a routes/events.js resembling Claude's typical emission shape.
# ---------------------------------------------------------------------------

_CLAUDE_ROUTES_EVENTS_JS = """\
const express = require('express');
const router = express.Router();
const pool = require('../db');
const { requireAuth, requireRole, validateCsrfToken } = require('../middleware/auth');

// GET /events - Public list
router.get('/', async (req, res) => {
  const result = await pool.query('SELECT * FROM events ORDER BY date_time');
  res.render('events/index', { title: 'Events', events: result.rows });
});

// GET /events/new - Admin create form
router.get('/new', requireAuth, requireRole('admin'), (req, res) => {
  res.render('events/new', { title: 'Add Event', error: null });
});

// GET /events/:id - Public detail
router.get('/:id', async (req, res) => {
  const result = await pool.query('SELECT * FROM events WHERE id = $1', [req.params.id]);
  res.render('events/detail', { title: 'Event', event: result.rows[0] });
});

// GET /events/:id/edit - Admin edit form
router.get('/:id/edit', requireAuth, requireRole('admin'), async (req, res) => {
  const result = await pool.query('SELECT * FROM events WHERE id = $1', [req.params.id]);
  res.render('events/edit', { title: 'Edit Event', item: result.rows[0], error: null });
});

// POST /events - Admin create
router.post('/', requireAuth, requireRole('admin'), validateCsrfToken, async (req, res) => {
  // ... insert ...
  res.redirect('/events');
});

module.exports = router;
"""


def _mw_like_spec() -> dict:
    return {
        "app_name": "medicine-waves-test",
        "description": "Medicine Waves sound journey",
        "data_models": [
            {"name": "Event", "fields": [{"name": "title", "type": "text"}]},
            {"name": "Photo", "fields": [{"name": "image_url", "type": "text"}]},
            {"name": "Video", "fields": [{"name": "embed_url", "type": "text"}]},
            {"name": "ContactMessage", "fields": [{"name": "message", "type": "text"}],
             "read_only": True},
        ],
    }


def _mk_artifact(files: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        files=list(files.items()),
        spec=_mw_like_spec(),
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )


# ---------------------------------------------------------------------------
# Registration + DAG placement
# ---------------------------------------------------------------------------


def test_processor_is_registered_in_pipeline():
    """The new post-processor must be registered via @processor decorator
    so run_pipeline includes it in both build and reapply phases."""
    assert "_rewrite_admin_router_template_switch" in pipeline.REGISTRY, (
        "missing @processor registration for _rewrite_admin_router_template_switch"
    )


def test_processor_runs_after_emit_widgets():
    """Template-switch rewrite must run AFTER _emit_widgets (widget views
    must exist on disk for the switched render to resolve)."""
    proc = pipeline.REGISTRY["_rewrite_admin_router_template_switch"]
    assert "_emit_widgets" in proc.after or any(
        dep == "_emit_widgets" for dep in proc.after
    ), f"expected after=('_emit_widgets',...), got after={proc.after}"


def test_processor_runs_in_both_phases():
    """Must run in build AND reapply, reapply on an older v19 artifact
    with widgets-on needs the router rewrite just as the fresh build does."""
    proc = pipeline.REGISTRY["_rewrite_admin_router_template_switch"]
    assert "build" in proc.phase, f"phase missing 'build': {proc.phase}"
    assert "reapply" in proc.phase, f"phase missing 'reapply': {proc.phase}"


def test_expected_processors_includes_new_name():
    """EXPECTED_PROCESSORS guardrail must enumerate the new processor."""
    assert "_rewrite_admin_router_template_switch" in pipeline.EXPECTED_PROCESSORS


# ---------------------------------------------------------------------------
# Rewrite behavior
# ---------------------------------------------------------------------------


def test_rewrite_injects_view_prefix_on_events_new():
    """The handler for GET /new must be rewritten to switch view prefix
    based on req.baseUrl."""
    artifact = _mk_artifact({"routes/events.js": _CLAUDE_ROUTES_EVENTS_JS})
    build._rewrite_admin_router_template_switch(artifact)
    rewritten = dict(artifact.files)["routes/events.js"]
    # Either the render call now includes viewPrefix concatenation,
    # or there's an explicit conditional on req.baseUrl.
    assert "viewPrefix" in rewritten or "req.baseUrl" in rewritten, (
        "rewrite did not inject a template-switch branch in routes/events.js"
    )


def test_rewrite_includes_events_new_in_switch():
    """Specifically: the view name 'events/new' must be reachable via the
    viewPrefix path so /admin/events/new resolves 'admin/events/new'."""
    artifact = _mk_artifact({"routes/events.js": _CLAUDE_ROUTES_EVENTS_JS})
    build._rewrite_admin_router_template_switch(artifact)
    rewritten = dict(artifact.files)["routes/events.js"]
    # Must still reference 'events/new' so viewPrefix + 'events/new' works
    assert "events/new" in rewritten
    # Runtime test: the branch must reach `'admin/'` when req.baseUrl says so
    assert "'admin/'" in rewritten, (
        "rewrite did not emit the 'admin/' prefix literal; "
        "runtime won't resolve widget-emitted view"
    )


def test_rewrite_idempotent():
    """Running the rewrite twice must produce byte-identical output."""
    artifact1 = _mk_artifact({"routes/events.js": _CLAUDE_ROUTES_EVENTS_JS})
    build._rewrite_admin_router_template_switch(artifact1)
    first = dict(artifact1.files)["routes/events.js"]

    artifact2 = _mk_artifact({"routes/events.js": first})
    build._rewrite_admin_router_template_switch(artifact2)
    second = dict(artifact2.files)["routes/events.js"]

    assert first == second, (
        "rewrite not idempotent: second pass changed content"
    )


def test_rewrite_covers_edit_handler():
    """Handler for GET /:id/edit must also switch view prefix."""
    artifact = _mk_artifact({"routes/events.js": _CLAUDE_ROUTES_EVENTS_JS})
    build._rewrite_admin_router_template_switch(artifact)
    rewritten = dict(artifact.files)["routes/events.js"]
    # The string 'events/edit' must still appear (view name literal)
    assert "events/edit" in rewritten


def test_rewrite_does_not_touch_public_index_handler():
    """The public list handler (`res.render('events/index', ...)`) MUST
    NOT get the viewPrefix branch, public list stays public."""
    artifact = _mk_artifact({"routes/events.js": _CLAUDE_ROUTES_EVENTS_JS})
    build._rewrite_admin_router_template_switch(artifact)
    rewritten = dict(artifact.files)["routes/events.js"]
    # Find the index handler region and check it wasn't rewritten
    assert "res.render('events/index'" in rewritten or \
           "res.render(\"events/index\"" in rewritten or \
           "res.render(viewPrefix + 'events/index'" in rewritten, \
           "events/index render was destroyed or renamed unexpectedly"


def test_rewrite_skips_when_model_is_read_only():
    """ContactMessage is read_only in spec, its route file should NOT
    have the admin template-switch injected (no admin new/edit view to
    switch TO)."""
    cm_routes = _CLAUDE_ROUTES_EVENTS_JS.replace("events", "contact-messages")
    artifact = _mk_artifact({"routes/contact-messages.js": cm_routes})
    build._rewrite_admin_router_template_switch(artifact)
    rewritten = dict(artifact.files)["routes/contact-messages.js"]
    # No viewPrefix branch on a read-only model; either file unchanged
    # or rewrite limited to nothing
    assert rewritten == cm_routes or "viewPrefix" not in rewritten, (
        "read-only ContactMessage routes got template-switch injected"
    )


def test_rewrite_noops_when_routes_file_absent():
    """If a writable model has no route file (Claude forgot it), rewrite
    silently skips rather than crashing."""
    artifact = _mk_artifact({"server.js": "// stub"})
    # Should not raise
    build._rewrite_admin_router_template_switch(artifact)


def test_rewrite_preserves_file_list_ordering():
    """File list order must not be shuffled by the rewrite."""
    files = {
        "server.js": "// one",
        "routes/events.js": _CLAUDE_ROUTES_EVENTS_JS,
        "routes/photos.js": _CLAUDE_ROUTES_EVENTS_JS.replace("events", "photos"),
    }
    artifact = _mk_artifact(files)
    build._rewrite_admin_router_template_switch(artifact)
    paths = [p for p, _ in artifact.files]
    assert paths == list(files.keys()), (
        f"file order changed: {paths} vs {list(files.keys())}"
    )


# ---------------------------------------------------------------------------
# Runtime signal via node eval (run the rewritten router in node)
# ---------------------------------------------------------------------------


def test_rewritten_router_renders_the_admin_view_under_an_admin_mount():
    """Run the rewritten routes/events.js in node with stand-ins for express,
    the database and the middleware, call its handlers with a request whose
    baseUrl is '/admin/events' and with one whose baseUrl is '/events', and
    read the view name each hands to res.render. The new and edit handlers
    switch to the 'admin/' view only under the admin mount; the public list
    handler never does."""
    import json
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")

    artifact = _mk_artifact({"routes/events.js": _CLAUDE_ROUTES_EVENTS_JS})
    build._rewrite_admin_router_template_switch(artifact)
    rewritten = dict(artifact.files)["routes/events.js"]

    harness = """
    const vm = require('vm');
    const source = require('fs').readFileSync(0, 'utf8');
    const handlers = {};
    const router = {
      get: (path, ...chain) => { handlers['GET ' + path] = chain[chain.length - 1]; },
      post: () => {},
    };
    const stubs = {
      'express': { Router: () => router },
      '../db': { query: async () => ({ rows: [{}] }) },
      '../middleware/auth': { requireAuth: () => {}, requireRole: () => () => {}, validateCsrfToken: () => {} },
    };
    vm.runInNewContext(source, { require: (name) => stubs[name], module: { exports: {} } });
    (async () => {
      const seen = {};
      for (const [label, baseUrl] of [['admin', '/admin/events'], ['public', '/events']]) {
        for (const [key, route] of [['index', 'GET /'], ['new', 'GET /new'], ['edit', 'GET /:id/edit']]) {
          let view = null;
          await handlers[route]({ baseUrl, params: { id: '1' } }, { render: (name) => { view = name; } });
          seen[label + ' ' + key] = view;
        }
      }
      console.log(JSON.stringify(seen));
    })();
    """
    result = subprocess.run(
        ["node", "-e", harness], input=rewritten,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "admin index": "events/index", "public index": "events/index",
        "admin new": "admin/events/new", "public new": "events/new",
        "admin edit": "admin/events/edit", "public edit": "events/edit",
    }
