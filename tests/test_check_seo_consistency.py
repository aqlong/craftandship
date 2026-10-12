"""Tests for scripts/check_seo_consistency.py.

These tests exercise the lint's pure helpers (comment stripping, route
extraction, banned-phrase scanning) and the end-to-end script behavior on
the live worktree (which should pass: that's the contract, if this lint
fails on master, the SEO surface drifted from the rest of the app).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'scripts' / 'check_seo_consistency.py'


@pytest.fixture(scope='module')
def lint_module():
    """Import scripts/check_seo_consistency.py as a module so we can call its helpers."""
    spec = importlib.util.spec_from_file_location('check_seo_consistency', SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ─── _strip_ts_comments ─────────────────────────────────────────────────

def test_strip_ts_strips_line_comments(lint_module):
    src = "const a = 1; // we don't verify\nconst b = 2;\n"
    out = lint_module._strip_ts_comments(src)
    # Line content survives; comment text is gone.
    assert 'const a = 1;' in out
    assert "we don't verify" not in out
    # Newline preserved so line numbers stay parity.
    assert out.count('\n') == src.count('\n')


def test_strip_ts_strips_block_comments_preserving_line_count(lint_module):
    src = "const a = 1;\n/* hostage\n   if we ever close */\nconst b = 2;\n"
    out = lint_module._strip_ts_comments(src)
    assert 'hostage' not in out
    assert 'if we ever close' not in out
    # Block comment spans 3 lines (\n inside it); newline parity preserved.
    assert out.count('\n') == src.count('\n')


def test_strip_ts_keeps_banned_phrases_inside_string_literals(lint_module):
    # If a banned phrase legitimately lives inside a string (e.g. a
    # rail-template refusal, an error message), the lint should still see
    # it. The brand-voice rule applies to customer-visible strings, not
    # source-code commentary.
    src = "const msg = 'we don\\'t verify or ask why';\n"
    out = lint_module._strip_ts_comments(src)
    assert "we don" in out


def test_strip_ts_handles_unterminated_block_comment(lint_module):
    src = "const a = 1;\n/* unterminated comment\nshould not crash"
    # Should not raise; should drop the comment content but keep newlines.
    out = lint_module._strip_ts_comments(src)
    assert 'unterminated' not in out


def test_strip_ts_handles_template_literal(lint_module):
    src = "const t = `template with hostage word inside`;\n"
    out = lint_module._strip_ts_comments(src)
    # Template literal contents survive (treated as a string).
    assert 'hostage' in out


# ─── _extract_routes ────────────────────────────────────────────────────

def test_extract_routes_finds_get_handlers(lint_module):
    src = """
    app.get('/foo', (req, res) => { res.send('foo'); });
    app.get('/bar/baz', (req, res) => { res.send('bar'); });
    app.use('/admin', adminRouter); // not a route literal, ignored
    app.get('/qux', myHandler);
    """
    routes = lint_module._extract_routes(src)
    assert routes == ['/foo', '/bar/baz', '/qux']


def test_extract_routes_handles_double_quotes(lint_module):
    src = 'app.get("/double-quoted", h);'
    assert lint_module._extract_routes(src) == ['/double-quoted']


# ─── _extract_sitemap_urls ──────────────────────────────────────────────

def test_extract_sitemap_urls_pulls_url_array(lint_module):
    src = """
    app.get('/sitemap.xml', (_req, res) => {
      const urls = [
        '/',
        '/pricing',
        '/about',
      ];
      res.type('application/xml').send(buildXml(urls));
    });
    """
    urls = lint_module._extract_sitemap_urls(src)
    assert urls == ['/', '/pricing', '/about']


def test_extract_sitemap_urls_returns_empty_when_route_missing(lint_module):
    assert lint_module._extract_sitemap_urls('// no sitemap route here') == []


# ─── _extract_pricing_literals ──────────────────────────────────────────

def test_extract_pricing_literals(lint_module):
    src = """
      res.locals.harborSubscriptionPrice = '$39';
      res.locals.harborSlidingScalePrice = '$19';
    """
    sub, sli = lint_module._extract_pricing_literals(src)
    assert sub == '$39'
    assert sli == '$19'


def test_extract_pricing_literals_missing_returns_empty(lint_module):
    sub, sli = lint_module._extract_pricing_literals('nothing relevant')
    assert sub == ''
    assert sli == ''


# ─── _is_public_route ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    'path,expected',
    [
        ('/', True),
        ('/pricing', True),
        ('/about', True),
        ('/about/ocean', True),
        ('/the-companion', True),
        ('/admin', False),
        ('/admin/builds', False),
        ('/api/health', False),
        ('/ops/api/abc', False),
        ('/webhooks/stripe', False),
        ('/login', False),
        ('/signup', False),
        ('/intake', False),
        ('/intake/abc', False),
        ('/lighthouse', False),
        ('/lighthouse/table', False),
        ('/sitemap.xml', False),
        ('/robots.txt', False),
        ('/llms.txt', False),
        ('/health', False),
    ],
)
def test_is_public_route(lint_module, path, expected):
    assert lint_module._is_public_route(path) is expected


# ─── End-to-end: lint passes on the live worktree ──────────────────────

def test_lint_passes_on_live_worktree():
    """The lint must pass on the current worktree state.

    If this test fails on master, the SEO surface drifted from the rest
    of the app (a public route was added without a sitemap entry, a
    banned phrase landed in a customer-facing source, pricing literals
    desynced from /llms.txt body, etc.). Read the failure output;
    it points directly at the file:line that needs fixing.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        pytest.fail(
            'check_seo_consistency.py failed on the live worktree.\n\n'
            f'stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}'
        )


# ─── End-to-end: synthetic failure cases ───────────────────────────────

def test_lint_flags_missing_sitemap_entry(lint_module, tmp_path, monkeypatch):
    """If a public route is added but not in the sitemap, the lint flags it."""
    fake_app = (
        "app.get('/', h);\n"
        "app.get('/pricing', h);\n"
        "app.get('/the-companion', h);\n"
        "app.get('/how-it-works', h);\n"
        "app.get('/gallery', h);\n"
        "app.get('/about', h);\n"
        "app.get('/about/ocean', h);\n"
        "app.get('/contact', h);\n"
        "app.get('/terms', h);\n"
        "app.get('/privacy', h);\n"
        "app.get('/journal/somatic-website', h);\n"  # NEW route, not in sitemap
        "app.get('/sitemap.xml', (_req, res) => {\n"
        "  const urls = ['/', '/pricing', '/the-companion', '/how-it-works', '/gallery', '/about', '/about/ocean', '/contact', '/terms', '/privacy'];\n"
        "  res.send(buildXml(urls));\n"
        "});\n"
    )
    errors = lint_module.check_sitemap_completeness(fake_app)
    assert any('/journal/somatic-website' in e for e in errors), (
        f'Expected sitemap-missing error for /journal/somatic-website, got: {errors}'
    )


def test_lint_flags_pricing_drift_in_llms_txt(lint_module):
    """If /llms.txt body lacks the canonical pricing literals, the lint flags it."""
    fake_app = (
        "res.locals.harborSubscriptionPrice = '$39';\n"
        "res.locals.harborSlidingScalePrice = '$19';\n"
        "app.get('/llms.txt', (_req, res) => {\n"
        "  res.type('text/plain').send(`# CAS\n\nNo prices mentioned here.`);\n"
        "});\n"
    )
    errors = lint_module.check_pricing_consistency(fake_app)
    assert any('$39' in e and 'llms.txt' in e for e in errors)
    assert any('$19' in e and 'llms.txt' in e for e in errors)
