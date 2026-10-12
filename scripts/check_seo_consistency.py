#!/usr/bin/env python3
"""
Lint that asserts the SEO surface stays consistent with the rest of the app.

What it checks
--------------
1. Every public Harbor route declared in `web/src/app.ts` (any `app.get('/...',
   ...)` whose path is not under `/admin`, `/api`, `/ops`, `/webhooks`, `/auth`,
   `/builds`, `/intake`, `/lighthouse`, `/feedback`, `/features`, `/payment`,
   `/gallery`, `/support`, `/uploads`, `/projects`, `/dev`, `/docs`, or a
   well-known internal path) appears in the sitemap URL list at
   `app.get('/sitemap.xml', ...)`.
2. The canonical pricing literals in `harborSubscriptionPrice` /
   `harborSlidingScalePrice` (web/src/app.ts) match the price line in
   `llms.txt` and the canonical sliding-scale phrasing in
   `web/src/services/pricing-faq.ts`.
3. `web/src/services/structured-data.ts` and
   `web/src/services/pricing-faq.ts` and `app.ts /llms.txt` body do NOT
   contain banned phrasings: shutdown talk ("if we ever close", "hostage",
   "lock-in"), calendar timetables ("7-day", "trial period"), competitor
   names on customer surfaces ("Mindbody", "Squarespace", "Wix",
   "Practice.do", "Lovable", "Bolt", "Webflow"), defensive negation of the
   trust gesture ("we don't verify", "we don't ask why").

Exit codes
----------
- 0: all checks pass.
- 1: at least one check failed; details printed to stderr.

Hooks
-----
Wired into `.githooks/pre-commit` to fire only when relevant files are
staged. Manual run: `python3 scripts/check_seo_consistency.py`.
`--root DIR` reads the same files under DIR instead of this checkout: the
pre-commit hook passes a directory that holds the staged content, so that the
lint judges what is being committed and not the working tree.

Why this script exists
----------------------
The SEO surface is small but easy to drift. New public routes get added
without sitemap updates; pricing is bumped in `app.ts` but the FAQ source
and llms.txt fall behind; a casual copy edit reintroduces banned phrasings
that the brand-voice rule (BRAND_VOICE.md Affirmative voice + 2026-04-28
no-shutdown-talk extension) retired. This script is the lint that catches
that drift.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_TS = REPO_ROOT / 'web' / 'src' / 'app.ts'
PRICING_FAQ = REPO_ROOT / 'web' / 'src' / 'services' / 'pricing-faq.ts'
STRUCTURED_DATA = REPO_ROOT / 'web' / 'src' / 'services' / 'structured-data.ts'
ABOUT_OCEAN = REPO_ROOT / 'web' / 'views' / 'about-ocean.ejs'

# Routes mounted under these prefixes are internal / authed / API surfaces;
# they are not expected in the sitemap. Add to this set as the app grows.
INTERNAL_ROUTE_PREFIXES = (
    '/admin', '/api', '/ops', '/webhooks', '/auth', '/builds', '/intake',
    '/lighthouse', '/feedback', '/features', '/payment', '/gallery/screenshots',
    '/support', '/uploads', '/projects', '/dev', '/docs', '/health',
    '/login', '/signup', '/logout', '/forgot-password', '/reset-password',
    '/verify-email', '/resend-verification', '/begin', '/try', '/profile',
    '/account', '/dashboard', '/set-password', '/anon', '/anon-intake',
    '/sitemap.xml',
    '/robots.txt', '/llms.txt', '/manifest.json', '/sw.js', '/og-default.png',
    '/brand-harbor', '/style.css', '/favicon', '/cas-trial-bar',
)

# Public Harbor surfaces we expect in the sitemap. Auth pages explicitly
# excluded above. Update if the IA changes.
EXPECTED_SITEMAP_PATHS = {
    '/', '/pricing', '/the-companion', '/how-it-works', '/gallery',
    '/about', '/about/ocean', '/contact', '/terms', '/privacy',
    '/guide', '/guide/read',
}

# Phrases that must never appear on customer-facing surfaces, per
# BRAND_VOICE.md § 2 (Affirmative voice) + 2026-04-28 extension.
# Each entry is (description, regex). Regexes are case-insensitive.
BANNED_PHRASES = [
    ('shutdown talk: "if we ever close"', r'\bif we ever (?:close|shut)\b'),
    ('shutdown talk: "hostage"', r'\bhostage\b'),
    ('shutdown talk: "lock-in" / "lock in"', r'\block[- ]?in\b'),
    ('calendar timetable: "7-day" / "seven-day"', r'\b(?:7|seven)[- ]?days?\b'),
    ('calendar timetable: "trial period"', r'\btrial period\b'),
    ('calendar timetable: "trial expires"', r'\btrial expires\b'),
    ('defensive negation: "we don\'t verify"', r"we don['’]t verify"),
    ('defensive negation: "we don\'t ask why"', r"we don['’]t ask why"),
    ('competitor name on customer surface: "Mindbody"', r'\bmindbody\b'),
    ('competitor name on customer surface: "Squarespace"', r'\bsquarespace\b'),
    ('competitor name on customer surface: "Wix"', r'\bwix\b'),
    ('competitor name on customer surface: "Practice.do"', r'\bpractice\.do\b'),
    ('competitor name on customer surface: "Lovable"', r'\blovable\b'),
    ('competitor name on customer surface: "Bolt"', r'\bbolt\b'),
    ('competitor name on customer surface: "Webflow"', r'\bwebflow\b'),
]

# Files scanned for banned-phrase violations. Customer-facing prose lives
# here. Internal docs / research / memory files are exempt.
COPY_FILES = [
    PRICING_FAQ,
    STRUCTURED_DATA,
    ABOUT_OCEAN,
]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return ''


def _strip_ts_comments(source: str) -> str:
    """Strip // line comments and /* ... */ block comments from a TS/JS body.

    Preserves line numbers so error messages keep pointing at the right
    spot in the original file. Aware of strings (does not strip a // that
    sits inside a string literal). Not perfect (no template-literal
    expression awareness, no regex-literal awareness), but good enough
    for the kind of brand-voice rule lint we run here. Matches the
    posture of scripts/check_brand_voice.py for Python.
    """
    out = []
    i = 0
    n = len(source)
    in_string = None  # None | "'" | '"' | '`'
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ''
        # Inside a string literal: pass through, but watch for the closer.
        if in_string is not None:
            if ch == '\\' and i + 1 < n:
                out.append(ch)
                out.append(source[i + 1])
                i += 2
                continue
            if ch == in_string:
                in_string = None
            out.append(ch)
            i += 1
            continue
        # Block comment: drop content but keep newlines for line-number parity.
        if ch == '/' and nxt == '*':
            j = source.find('*/', i + 2)
            if j == -1:
                # Unterminated: drop the rest, keep newlines.
                tail = source[i + 2:]
                out.append('\n' * tail.count('\n'))
                break
            chunk = source[i + 2:j]
            out.append('\n' * chunk.count('\n'))
            i = j + 2
            continue
        # Line comment: drop content up to (but not including) the newline.
        if ch == '/' and nxt == '/':
            j = source.find('\n', i + 2)
            if j == -1:
                break
            i = j  # leave the \n to the next iteration
            continue
        # Enter a string literal.
        if ch in ("'", '"', '`'):
            in_string = ch
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _extract_routes(app_ts: str) -> list[str]:
    """Pull every `app.get('/path', ...)` literal from app.ts."""
    return [m.group(1) for m in re.finditer(
        r"app\.get\(\s*['\"]([^'\"]+)['\"]", app_ts
    )]


def _extract_sitemap_urls(app_ts: str) -> list[str]:
    """Pull the sitemap URL list. The route hands a `urls` array literal."""
    m = re.search(
        r"app\.get\(\s*['\"]/sitemap\.xml['\"][\s\S]*?const\s+urls\s*=\s*\[([\s\S]*?)\]",
        app_ts,
    )
    if not m:
        return []
    body = m.group(1)
    return re.findall(r"['\"]([^'\"]+)['\"]", body)


def _extract_llms_txt_body(app_ts: str) -> str:
    """Pull the llms.txt template-literal body."""
    m = re.search(
        r"app\.get\(\s*['\"]/llms\.txt['\"][\s\S]*?send\(\s*`([\s\S]*?)`\s*\)\s*;",
        app_ts,
    )
    return m.group(1) if m else ''


def _extract_pricing_literals(app_ts: str) -> tuple[str, str]:
    """Pull harborSubscriptionPrice + harborSlidingScalePrice."""
    sub = re.search(r"harborSubscriptionPrice\s*=\s*['\"]([^'\"]+)['\"]", app_ts)
    sli = re.search(r"harborSlidingScalePrice\s*=\s*['\"]([^'\"]+)['\"]", app_ts)
    return (sub.group(1) if sub else '', sli.group(1) if sli else '')


def _is_public_route(path: str) -> bool:
    if not path.startswith('/'):
        return False
    if path == '/':
        return True
    for prefix in INTERNAL_ROUTE_PREFIXES:
        if path == prefix or path.startswith(prefix + '/') or path == prefix + '.xml' or path == prefix + '.txt':
            return False
        # Also handle exact-match for prefixes that don't have trailing slash
        if path == prefix:
            return False
    return True


def check_sitemap_completeness(app_ts: str) -> list[str]:
    """Every public Harbor route must appear in the sitemap."""
    errors = []
    routes = [p for p in _extract_routes(app_ts) if _is_public_route(p)]
    sitemap_urls = set(_extract_sitemap_urls(app_ts))
    for path in routes:
        if path not in sitemap_urls:
            errors.append(
                f'sitemap missing public route: {path} '
                f'(declared in app.get but not in /sitemap.xml urls list)'
            )
    # Also flag if any expected path is missing; defends against an
    # accidental urls[] edit that drops a known-good page.
    for expected in EXPECTED_SITEMAP_PATHS:
        if expected not in sitemap_urls:
            errors.append(f'sitemap missing canonical Harbor path: {expected}')
    return errors


def check_pricing_consistency(app_ts: str) -> list[str]:
    """Pricing literal in app.ts must appear in llms.txt + faq."""
    errors = []
    sub, sli = _extract_pricing_literals(app_ts)
    if not sub or not sli:
        errors.append(
            'pricing literals (harborSubscriptionPrice / harborSlidingScalePrice) '
            'not found in web/src/app.ts'
        )
        return errors
    llms = _extract_llms_txt_body(app_ts)
    if llms and sub not in llms:
        errors.append(
            f'llms.txt does not mention the canonical subscription price {sub} '
            f'(check /llms.txt route in web/src/app.ts)'
        )
    if llms and sli not in llms:
        errors.append(
            f'llms.txt does not mention the canonical sliding-scale floor {sli}'
        )
    # The pricing FAQ source uses interpolation, but the static phrase
    # "Choose any amount from <sub> down to <sli>" lives nowhere except
    # the views (pricing.ejs + index.ejs). Skip enforcing that here; the
    # route-level test in web/tests/unit/routes.seo.test.ts covers it.
    return errors


def check_banned_phrases() -> list[str]:
    """No defensive / negative / timetable phrases in customer surfaces.

    Scope is the rendered customer text, not source-code commentary. For
    .ts files we strip line + block comments first so that explanatory
    comments which quote banned phrases as "do not write this" examples
    don't trigger the lint. EJS files are not stripped; EJS comments
    use `<%# %>` and don't contain banned phrasings in practice.
    """
    errors = []
    for path in COPY_FILES:
        body = _read(path)
        if not body:
            continue
        scan_body = _strip_ts_comments(body) if path.suffix in ('.ts', '.js') else body
        for label, pattern in BANNED_PHRASES:
            for match in re.finditer(pattern, scan_body, flags=re.IGNORECASE):
                line_no = scan_body[: match.start()].count('\n') + 1
                errors.append(
                    f'{path.relative_to(REPO_ROOT)}:{line_no}: {label} -- '
                    f'matched "{match.group(0)}"'
                )
    # Also scan the llms.txt body inside app.ts (not in COPY_FILES because
    # it's a template literal, not a standalone file). The template literal
    # is itself customer-visible content; comments around it are not.
    app_ts_body = _read(APP_TS)
    llms = _extract_llms_txt_body(app_ts_body)
    if llms:
        for label, pattern in BANNED_PHRASES:
            for match in re.finditer(pattern, llms, flags=re.IGNORECASE):
                errors.append(
                    f'web/src/app.ts (/llms.txt body): {label} -- '
                    f'matched "{match.group(0)}"'
                )
    return errors


def use_root(root: Path) -> None:
    """Read the files under `root` instead of the checkout this script is in."""
    global REPO_ROOT, APP_TS, PRICING_FAQ, STRUCTURED_DATA, ABOUT_OCEAN, COPY_FILES
    REPO_ROOT = root
    APP_TS = root / 'web' / 'src' / 'app.ts'
    PRICING_FAQ = root / 'web' / 'src' / 'services' / 'pricing-faq.ts'
    STRUCTURED_DATA = root / 'web' / 'src' / 'services' / 'structured-data.ts'
    ABOUT_OCEAN = root / 'web' / 'views' / 'about-ocean.ejs'
    COPY_FILES = [PRICING_FAQ, STRUCTURED_DATA, ABOUT_OCEAN]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['--root'] and len(argv) == 2:
        use_root(Path(argv[1]).resolve())
    elif argv:
        print('usage: check_seo_consistency.py [--root DIR]', file=sys.stderr)
        return 2
    if not APP_TS.exists():
        print(f'fatal: {APP_TS} not found', file=sys.stderr)
        return 2
    app_ts = _read(APP_TS)

    all_errors: list[str] = []
    all_errors.extend(check_sitemap_completeness(app_ts))
    all_errors.extend(check_pricing_consistency(app_ts))
    all_errors.extend(check_banned_phrases())

    if all_errors:
        print('SEO consistency check failed:', file=sys.stderr)
        for err in all_errors:
            print(f'  - {err}', file=sys.stderr)
        print('', file=sys.stderr)
        print('See the docstring at the top of scripts/check_seo_consistency.py for what each check covers.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
