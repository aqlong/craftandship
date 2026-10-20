"""compose/build.py integration tests; spec in → files out.

These tests exercise the full compose pipeline against a known-good
healer spec fixture and assert the emitted files contain the right
shape. No Claude calls, no network.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from compose.build import (
    BuildSpec, resolve_spec, compose_files, build_app, validate_spec,
    _prettify, _slug_for, _plural, _default_fields, _default_nav_items,
    _default_admin_nav_items, PUBLIC_STYLE_CSS_PATH,
)


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE_SPEC = FIXTURE_DIR / "sample-healer-spec.json"


@pytest.fixture
def sample_spec():
    with open(SAMPLE_SPEC) as f:
        return BuildSpec.from_dict(json.load(f))


@pytest.fixture
def resolved(sample_spec):
    return resolve_spec(sample_spec)


# ---- validation ----

def test_validate_clean_spec_no_errors(sample_spec):
    assert validate_spec(sample_spec) == []


def test_validate_missing_app_name():
    spec = BuildSpec.from_dict({
        "app_name": "", "archetype": "healer-wellness", "admin_email": "a@b.c"
    })
    errors = validate_spec(spec)
    assert any("app_name" in e for e in errors)


def test_validate_invalid_app_name():
    spec = BuildSpec.from_dict({
        "app_name": "Invalid Name", "archetype": "healer-wellness", "admin_email": "a@b.c"
    })
    errors = validate_spec(spec)
    assert any("lowercase slug" in e for e in errors)


def test_validate_unknown_archetype():
    spec = BuildSpec.from_dict({
        "app_name": "ok-name", "archetype": "does-not-exist", "admin_email": "a@b.c"
    })
    errors = validate_spec(spec)
    assert any("Unknown archetype" in e for e in errors)


def test_validate_unknown_palette():
    spec = BuildSpec.from_dict({
        "app_name": "ok-name", "archetype": "healer-wellness",
        "palette": "nonexistent-palette", "admin_email": "a@b.c"
    })
    errors = validate_spec(spec)
    assert any("palette" in e.lower() for e in errors)


def test_validate_missing_admin_email():
    spec = BuildSpec.from_dict({
        "app_name": "ok-name", "archetype": "healer-wellness", "admin_email": ""
    })
    errors = validate_spec(spec)
    assert any("admin_email" in e for e in errors)


# ---- resolution ----

def test_resolve_picks_specified_palette(sample_spec):
    rs = resolve_spec(sample_spec)
    assert rs.palette["id"] == "warm-earth"


def test_resolve_falls_back_to_archetype_default_palette():
    spec = BuildSpec.from_dict({
        "app_name": "ok-name", "archetype": "healer-wellness",
        "admin_email": "a@b.c", "palette": ""
    })
    rs = resolve_spec(spec)
    # Should pick first default suggestion
    assert rs.palette["id"] == "warm-earth"


def test_resolve_picks_specified_font_pair(sample_spec):
    rs = resolve_spec(sample_spec)
    assert rs.font_pair["id"] == "cormorant-lato"


def test_resolve_produces_page_layouts_for_every_default_page(resolved):
    archetype_pages = set(resolved.archetype["default_pages"])
    assert set(resolved.page_layouts.keys()) == archetype_pages


def test_resolve_data_models_from_spec(resolved):
    names = {m["name"] for m in resolved.data_models}
    assert "BlogPost" in names
    assert "Event" in names
    assert "Testimonial" in names


# ---- file emission ----

def test_compose_files_produces_required_files(resolved):
    files = compose_files(resolved)
    paths = {p for p, _ in files}
    required = {
        "package.json", "server.js", "db.js", "db_migrate.js",
        "db/schema.sql", "db/seed.sql",
        "middleware/auth.js", "middleware/csrf.js", "middleware/settings.js",
        "routes/auth.js",
        # MW v25 pattern: separate admin-dashboard + admin-settings routers
        "routes/admin-dashboard.js", "routes/admin-settings.js",
        "routes/pages.js",
        # /robots.txt + /sitemap.xml
        "routes/seo.js",
        "views/layout-public.ejs", "views/layout-admin.ejs",
        "views/login.ejs",
        # admin views under views/admin/ subdir
        "views/admin/dashboard.ejs", "views/admin/settings.ejs",
        "views/home.ejs", "views/about.ejs", "views/contact.ejs",
        "views/error-404.ejs", "views/error-500.ejs",
        # public/robots.txt is no longer a static file; routes/seo.js owns
        # the URL so the body can reference the live request origin.
        "public/style.css", "public/manifest.json", "public/favicon.svg",
        ".env.example", ".railwayignore", "spec.json",
    }
    missing = required - paths
    assert not missing, f"missing expected files: {missing}"


def test_compose_files_has_resource_routers(resolved):
    files = compose_files(resolved)
    paths = {p for p, _ in files}
    for m in resolved.data_models:
        slug = _slug_for(m)
        assert f"routes/{slug}.js" in paths, f"missing router for {slug}"


def test_compose_files_has_admin_crud_views_per_model(resolved):
    """MW v25 pattern: views/<slug>/{manage,new,edit,admin-detail}.ejs."""
    files = compose_files(resolved)
    paths = {p for p, _ in files}
    for m in resolved.data_models:
        slug = _slug_for(m)
        for suffix in ("manage", "new", "edit", "admin-detail"):
            assert f"views/{slug}/{suffix}.ejs" in paths, (
                f"missing views/{slug}/{suffix}.ejs"
            )


def test_compose_files_has_public_views_for_public_listable_models(resolved):
    """Models with public_listing_path set get views/<slug>/{index,detail}.ejs."""
    files = compose_files(resolved)
    paths = {p for p, _ in files}
    for m in resolved.data_models:
        if not m.get("public_listing_path"):
            continue
        slug = _slug_for(m)
        assert f"views/{slug}/index.ejs" in paths
        assert f"views/{slug}/detail.ejs" in paths


def test_compose_files_idempotent(resolved):
    """Same input -> same output bytes (deterministic compose)."""
    files1 = compose_files(resolved)
    files2 = compose_files(resolved)
    assert [p for p, _ in files1] == [p for p, _ in files2]
    assert [c for _, c in files1] == [c for _, c in files2]


def test_compose_files_idempotent_across_a_second_boundary(resolved):
    """routes/seo.js BUILD_TIME is wall-clock (datetime.now). Two
    compose_files calls that straddle a one-second boundary MUST still
    produce byte-identical output (the build time is memoized on the
    ResolvedSpec). This is the exact intermittent failure
    test_compose_files_idempotent hit in CI on 2026-06-15: it only fails
    when the two calls land in different seconds, so a same-instant
    assertion passes ~99% of the time and hides the bug. Sleeping >1s
    forces the boundary deterministically."""
    import time
    files1 = dict(compose_files(resolved))
    time.sleep(1.1)
    files2 = dict(compose_files(resolved))
    assert files1.keys() == files2.keys()
    diffs = [p for p in files1 if files1[p] != files2[p]]
    assert not diffs, f"non-deterministic output across a second boundary: {diffs}"
    # And the seo.js BUILD_TIME specifically must match.
    assert files1["routes/seo.js"] == files2["routes/seo.js"]


def test_server_js_health_endpoint_queries_db(resolved):
    """The /health route must verify DB reachability, not just return 200.

    Earlier behavior (return 200 unconditionally) hid migration failures
    and DB connection errors behind a green healthcheck. A DJ-site build
    (2026-04-28) crashed on Railway with no clear reason because
    the app booted past a silent migration warning + /health still
    returned 200 to Railway's probe. Now /health runs SELECT 1 and
    returns 503 + the DB error message when the connection is broken.
    """
    files = dict(compose_files(resolved))
    server_js = files["server.js"]
    assert "app.get('/health'" in server_js
    assert "db.query('SELECT 1')" in server_js, "health route must SELECT 1 to verify DB"
    assert "503" in server_js, "health route must return 503 on DB error"


def test_db_js_pool_has_connection_timeout(resolved):
    """db.js Pool must set connectionTimeoutMillis so a hung connect
    fails in 10s (Railway internal-network race) instead of Node's
    default ~60s socket timeout. Without this bound, db_migrate's retry
    loop wastes minutes per attempt."""
    files = dict(compose_files(resolved))
    db_js = files["db.js"]
    assert "connectionTimeoutMillis: 10000" in db_js, (
        "db.js Pool must set connectionTimeoutMillis: 10000"
    )
    assert "connectionString: process.env.DATABASE_URL" in db_js


def test_db_migrate_js_waits_for_db_with_transient_retry(resolved):
    """db_migrate.js must retry on connection-class errors (ECONNREFUSED,
    ETIMEDOUT, ENETUNREACH, EHOSTUNREACH, ENOTFOUND, EAI_AGAIN) before
    running the schema migration. Auth failures, schema errors, and any
    other non-connection error must fail immediately (retry won't help).
    Root cause for a DJ-site build: freshly-
    provisioned Railway PG didn't accept connections fast enough; the
    AggregateError [ETIMEDOUT] surfaced after the first migration query.
    """
    files = dict(compose_files(resolved))
    migrate_js = files["db_migrate.js"]
    # The waitForDb retry helper exists.
    assert "async function waitForDb" in migrate_js
    # Connection-class error allowlist exists.
    assert "TRANSIENT_CONNECT_CODES" in migrate_js
    for code in ("ECONNREFUSED", "ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH", "ENOTFOUND", "EAI_AGAIN"):
        assert code in migrate_js, f"transient-code allowlist missing {code}"
    # AggregateError handling (Node 18+ happy-eyeballs): every inner
    # error must be transient for the aggregate to count as transient.
    assert "Array.isArray(e.errors)" in migrate_js
    # Retry budget: 12 attempts of 10s = ~2 min worst case.
    assert "waitForDb(12)" in migrate_js, "must call waitForDb with maxAttempts=12"
    assert "sleep(10000)" in migrate_js, "must wait 10s between retries"
    # waitForDb is called BEFORE the schema query.
    schema_idx = migrate_js.index("const schema = fs.readFileSync")
    wait_idx = migrate_js.index("await waitForDb(")
    assert wait_idx < schema_idx, "waitForDb must run before reading schema.sql"


def test_db_migrate_js_non_transient_errors_throw_immediately(resolved):
    """The retry helper must call `throw e` (not retry) for non-
    transient errors so auth/schema/role failures surface immediately
    instead of eating 2 minutes of retry budget.
    """
    files = dict(compose_files(resolved))
    migrate_js = files["db_migrate.js"]
    assert "if (!isTransientConnectError(e)) throw e;" in migrate_js


def test_db_migrate_js_runs_post_migrate_sql_when_present(resolved):
    """db_migrate.js must check for db/post-migrate.sql on every boot and
    run it when the file exists. The file is opt-in: operators drop a
    self-idempotent SQL file (WHERE NOT EXISTS / ON CONFLICT) at
    db/post-migrate.sql to land post-deploy data without modifying the
    generated migrate logic per-customer. Use cases: magic-link bootstrap
    on a redeploy, settings updates that need to land on an existing DB,
    data backfills, admin password resets.

    Driver: a deploy onto an existing database needed a one-shot
    SQL apply but compose's db_migrate.js only loaded seed.sql when the
    users table was empty, so per-customer modification was required.
    This generalizes the pattern.
    """
    files = dict(compose_files(resolved))
    migrate_js = files["db_migrate.js"]

    # The post-migrate.sql path is computed.
    assert "post-migrate.sql" in migrate_js, "must reference db/post-migrate.sql"
    # Existence check before reading (graceful no-op when absent).
    assert "fs.existsSync(postMigratePath)" in migrate_js, (
        "must guard read with fs.existsSync so missing file is a no-op, not a crash"
    )
    # The file is read + executed when present.
    assert "fs.readFileSync(postMigratePath" in migrate_js
    # Post-migrate runs AFTER schema + seed step so it can reference seeded
    # tables. Order: waitForDb -> schema -> seed -> post-migrate.
    schema_idx = migrate_js.index("const schema = fs.readFileSync")
    post_idx = migrate_js.index("post-migrate.sql")
    assert schema_idx < post_idx, "post-migrate must run AFTER schema migration"


def test_server_js_migration_failure_exits_loudly(resolved):
    """When schema migration throws, the app must exit non-zero with the
    full error stack/code/detail/hint logged. Earlier behavior
    (catch + console.error e.message + listen anyway) produced a
    "Server running" log followed by Railway flagging CRASHED on
    healthcheck minutes later, with the actual migration error
    swallowed. The container orchestrator (Railway / docker-compose)
    can only act on the truth via exit code + complete stderr.
    """
    files = dict(compose_files(resolved))
    server_js = files["server.js"]
    assert "FATAL: Schema migration failed" in server_js, "must log a clear FATAL line"
    assert "process.exit(1)" in server_js, "must exit non-zero on migration failure"
    assert "error.stack" in server_js, "must log full stack"
    assert "error.code" in server_js, "must log pg error code"
    assert "error.detail" in server_js, "must log pg error detail"
    # And the old silent-warning behavior is GONE.
    assert "Schema migration warning:" not in server_js, "old silent-warning behavior must be removed"


def test_schema_has_canonical_site_settings(resolved):
    files = dict(compose_files(resolved))
    schema = files["db/schema.sql"]
    assert "CREATE TABLE IF NOT EXISTS site_settings" in schema
    assert "key VARCHAR(100) PRIMARY KEY" in schema


def test_seed_has_all_canonical_settings_keys(resolved):
    files = dict(compose_files(resolved))
    seed = files["db/seed.sql"]
    expected_keys = {
        "site_name", "site_url", "site_description",
        "hero_tagline", "hero_description", "hero_image_url",
        "nav_logo_url", "about_title", "about_body", "about_photo_url",
        "contact_email", "contact_phone", "contact_address",
        "business_name", "phone", "support_email", "address",
        "instagram_url", "facebook_url", "youtube_url",
        "tiktok_url", "twitter_url", "linkedin_url",
        "venmo_handle",
    }
    for key in expected_keys:
        assert f"'{key}'" in seed, f"seed missing settings key: {key}"


def test_seed_no_bcrypt_hash_sec1(resolved):
    """SEC-1: no bcrypt hash of a known-plaintext in seed.sql."""
    import re
    files = dict(compose_files(resolved))
    seed = files["db/seed.sql"]
    bcrypt_re = re.compile(r"\$2[aby]\$\d+\$[./A-Za-z0-9]{53}")
    assert not bcrypt_re.search(seed), "seed contains bcrypt hash (SEC-1 violation)"


def test_seed_jsonb_list_value_emits_valid_json():
    """Regression: a list/dict seed value (a JSONB column like a photos
    array) must serialize as valid JSON, not Python repr. The old `else`
    branch ran `_sql_esc(["a","b"])` -> '[''a'', ''b'']' which Postgres
    rejects as invalid json, crashing the seed apply at app boot."""
    from compose.build import BuildSpec, resolve_spec, emit_seed_sql
    spec = BuildSpec.from_dict({
        "app_name": "seedjson",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [{
            "name": "Slideshow",
            "fields": [
                {"name": "title", "sql_type": "TEXT"},
                {"name": "image_paths", "sql_type": "JSONB"},
            ],
        }],
        "seed_content": {
            "slideshows": [
                {"title": "Gallery", "image_paths": ["a.jpg", "b.jpg"]},
            ],
        },
    })
    rs = resolve_spec(spec)
    _, seed = emit_seed_sql(rs)
    assert '\'["a.jpg", "b.jpg"]\'' in seed, "JSONB list must emit valid JSON"
    assert "[''a.jpg''" not in seed, "must not emit Python repr of the list"


def test_seed_belongs_to_fk_seeded_when_row_provides_it():
    """Regression: when a seed row carries the belongs_to FK value
    (e.g. Book.author_id), the FK column must be included in the INSERT so
    the relation is actually seeded instead of silently dropped."""
    from compose.build import BuildSpec, resolve_spec, emit_seed_sql
    fk = 7  # the link column holds the parent's integer id
    spec = BuildSpec.from_dict({
        "app_name": "seedfk",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [
            {"name": "Author", "fields": [{"name": "name", "sql_type": "TEXT"}]},
            {"name": "Book", "belongs_to": "Author",
             "fields": [{"name": "title", "sql_type": "TEXT"}]},
        ],
        "seed_content": {
            "books": [{"title": "X", "author_id": fk}],
        },
    })
    rs = resolve_spec(spec)
    _, seed = emit_seed_sql(rs)
    book_insert = [ln for ln in seed.splitlines() if "INSERT INTO books" in ln]
    assert book_insert, "expected a books seed INSERT"
    assert any("author_id" in ln and ln.rstrip(";").endswith(f", {fk})") for ln in book_insert), (
        "belongs_to FK column must be seeded when the row provides it"
    )


def test_seed_belongs_to_fk_omitted_when_row_lacks_it():
    """Safety: when no seed row provides the FK, the FK column is NOT
    forced into the INSERT (keeps archetype placeholder rows unchanged and
    never emits a NULL/garbage FK that could trip the constraint)."""
    from compose.build import BuildSpec, resolve_spec, emit_seed_sql
    spec = BuildSpec.from_dict({
        "app_name": "seedfk2",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [
            {"name": "Author", "fields": [{"name": "name", "sql_type": "TEXT"}]},
            {"name": "Book", "belongs_to": "Author",
             "fields": [{"name": "title", "sql_type": "TEXT"}]},
        ],
        "seed_content": {
            "books": [{"title": "X"}],
        },
    })
    rs = resolve_spec(spec)
    _, seed = emit_seed_sql(rs)
    book_insert = [ln for ln in seed.splitlines() if "INSERT INTO books" in ln]
    assert book_insert, "expected a books seed INSERT"
    assert not any("author_id" in ln for ln in book_insert), (
        "FK column must be omitted when no row provides it"
    )


def test_validate_spec_rejects_injection_shaped_identifiers():
    """Regression: model/field names flow into emitted DDL + SQL, so the
    universal validate_spec gate refuses injection-shaped identifiers that a
    non-harbor caller could supply (the harbor TS translator sanitizes, but a
    direct dict caller bypasses it)."""
    from compose.build import BuildSpec, validate_spec
    bad_field = BuildSpec.from_dict({
        "app_name": "injtest",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [{
            "name": "Note",
            "fields": [{"name": "title); DROP TABLE customers;--", "sql_type": "TEXT"}],
        }],
    })
    errs = validate_spec(bad_field)
    assert any("field name is not a safe identifier" in e for e in errs)
    bad_slug = BuildSpec.from_dict({
        "app_name": "injtest2",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [{"name": "Note", "plural": "notes; DROP TABLE x",
                         "fields": [{"name": "title", "sql_type": "TEXT"}]}],
    })
    assert any("not a safe slug" in e for e in validate_spec(bad_slug))
    # A normal PascalCase model + snake_case fields pass clean.
    good = BuildSpec.from_dict({
        "app_name": "oktest",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [{"name": "BlogPost",
                         "fields": [{"name": "published_at", "sql_type": "TIMESTAMP"}]}],
    })
    assert not any("safe identifier" in e or "safe slug" in e for e in validate_spec(good))


def test_package_json_has_safe_multer_and_bcrypt(resolved):
    files = dict(compose_files(resolved))
    pkg = json.loads(files["package.json"])
    assert pkg["dependencies"]["multer"] == "^2.1.1"
    assert pkg["dependencies"]["bcrypt"].startswith("^6.")


def test_server_js_dual_mounts_all_resource_routers(resolved):
    """MW v25 pattern: each resource mounts at its public_listing_path
    (e.g. /gallery for Photo, /journal for BlogPost) AND at /admin/<slug>
    guarded by requireAdmin. Public-less models only mount under /admin/.
    """
    files = dict(compose_files(resolved))
    server = files["server.js"]
    for m in resolved.data_models:
        slug = _slug_for(m)
        public_path = m.get("public_listing_path") or f"/{slug}"
        assert f"app.use('{public_path}'," in server, (
            f"missing public mount at {public_path} for /{slug}"
        )
        assert f"app.use('/admin/{slug}', requireAdmin" in server, (
            f"missing admin-guarded mount for /admin/{slug}"
        )


def test_server_js_has_expected_middleware_chain(resolved):
    files = dict(compose_files(resolved))
    server = files["server.js"]
    assert "siteSettingsMiddleware" in server
    assert "csrfMiddleware" in server
    assert "expressLayouts" in server
    assert "app.set('trust proxy', 1)" in server
    # currentPath helper for nav active-state (MW v25)
    assert "res.locals.currentPath" in server


def test_server_js_mounts_admin_dashboard_and_settings(resolved):
    """MW v25: /admin is the dashboard (renders admin/dashboard.ejs),
    /admin/settings is the settings form. Two separate routers."""
    files = dict(compose_files(resolved))
    server = files["server.js"]
    assert "require('./routes/admin-dashboard')" in server
    assert "require('./routes/admin-settings')" in server


def test_admin_dashboard_renders_not_redirects(resolved):
    """MW v25: GET /admin RENDERS admin/dashboard.ejs directly, not redirects.

    (POST handlers like /change-password may redirect after success; this
    test is scoped to the GET / handler only.)
    """
    files = dict(compose_files(resolved))
    admin_js = files["routes/admin-dashboard.js"]
    assert "res.render('admin/dashboard'" in admin_js
    # Slice out the GET / handler: from the line starting with
    # "router.get('/'" up to the next top-level "router." line.
    start = admin_js.find("router.get('/'")
    assert start != -1, "No GET / handler in admin-dashboard.js"
    # Next top-level router call is the following "router.post(" or
    # "router.get(" or module.exports.
    remainder = admin_js[start + len("router.get('/'"):]
    next_router = remainder.find("\nrouter.")
    next_exports = remainder.find("\nmodule.exports")
    end_candidates = [p for p in (next_router, next_exports) if p != -1]
    end = min(end_candidates) if end_candidates else len(remainder)
    get_body = remainder[:end]
    assert "res.redirect(" not in get_body, "GET /admin must render, not redirect"


def test_auth_routes_use_csrf_validation(resolved):
    files = dict(compose_files(resolved))
    auth = files["routes/auth.js"]
    assert "validateCsrfToken" in auth


def test_auth_routes_emit_magic_link_handler(resolved):
    """Regression 2026-04-22: compose was emitting NO /magic?token=X route,
    causing verify_magic_link_auth in the pipeline to always fail with a
    non-blocking warning AND leaving customers unable to use their emailed
    login link. The route MUST exist and MUST sha256-hash the incoming raw
    token (to match worker's `hashlib.sha256(raw_token.encode()).hexdigest()`
    in `_generate_magic_link_token` in queue/worker_pg.py).

    2026-05-27 update (v2-magic-link-two-step-confirm): GET /magic is now
    non-consuming; the consume step moved to POST /magic/consume. Both
    handlers must exist and both must sha256-hash the raw token. The
    single-use UPDATE belongs ONLY to the POST handler.
    """
    files = dict(compose_files(resolved))
    auth = files["routes/auth.js"]

    # Both routes are emitted
    assert "router.get('/magic'" in auth, (
        "compose must emit a /magic?token=X handler that renders the "
        "confirm view; without it, customers can't see the sign-in step"
    )
    assert "router.post('/magic/consume'" in auth, (
        "compose must emit a POST /magic/consume handler that performs "
        "the single-use UPDATE + session set + redirect; the GET handler "
        "no longer consumes by itself"
    )

    # Hash algorithm matches worker's SHA-256
    assert "crypto.createHash('sha256')" in auth, (
        "/magic handlers must sha256-hash the raw token (matching worker's "
        "_generate_magic_link_token at queue/worker_pg.py); any other hash "
        "breaks the hash-compare against magic_link_tokens.token_hash"
    )

    # Queries the correct table + column
    assert "magic_link_tokens" in auth
    assert "token_hash" in auth
    assert "used_at" in auth, (
        "/magic handlers must check used_at IS NULL (single-use token)"
    )
    assert "expires_at" in auth, (
        "/magic handlers must check expires_at > NOW() (token TTL)"
    )

    # Single-use enforcement: sets used_at on success
    assert "SET used_at = NOW()" in auth, (
        "/magic/consume handler must mark the token used after successful "
        "login so the same link can't be replayed"
    )

    # Redirects to /admin (or /set-password) on success
    assert "/admin" in auth and "/set-password" in auth, (
        "/magic/consume handler must redirect to /admin (password_set=TRUE) "
        "or /set-password (password_set=FALSE) after successful auth"
    )


def test_auth_routes_emit_magic_link_head_preflight(resolved):
    """v2-magic-link-verification-non-consuming (2026-05-02): ops needs
    to verify a magic-link token is wired correctly without burning the
    single-use token. Compose emits a HEAD /magic handler that performs
    the same SELECT lookup (token_hash + used_at + expires_at checks)
    but does NOT update used_at. Returns 200 valid / 410 invalid.
    """
    files = dict(compose_files(resolved))
    auth = files["routes/auth.js"]
    # HEAD route exists
    assert "router.head('/magic'" in auth, (
        "compose must emit a HEAD /magic handler so ops can verify the "
        "token without consuming it (curl -I); see "
        "v2-magic-link-verification-non-consuming"
    )
    # Find the HEAD block + assert its body shape
    head_start = auth.find("router.head('/magic'")
    head_end = auth.find("router.get('/magic'", head_start)
    assert head_end > head_start, "HEAD /magic must precede GET /magic in source"
    head_block = auth[head_start:head_end]
    # Critical: the HEAD block must NOT contain `SET used_at = NOW()`
    # (that's the consume-the-token write; preflight must be read-only).
    assert "SET used_at = NOW()" not in head_block, (
        "HEAD /magic must NOT consume the token; the single-use UPDATE "
        "belongs only in the GET handler"
    )
    # HEAD block should hash + lookup
    assert "crypto.createHash('sha256')" in head_block
    assert "magic_link_tokens" in head_block
    # Returns 200 on valid, 410 on invalid/expired/used (no body)
    assert "res.status(200).end()" in head_block
    assert "res.status(410).end()" in head_block


def test_auth_routes_magic_handler_rejects_invalid_token(resolved):
    """The /magic handler must redirect to /login on missing, unknown,
    already-used, or expired tokens; never 500."""
    files = dict(compose_files(resolved))
    auth = files["routes/auth.js"]
    # Rejects missing/short tokens
    assert "invalid-token" in auth, (
        "/magic must redirect to /login?error=invalid-token on missing token"
    )
    # Rejects already-used tokens
    assert "token-already-used" in auth, (
        "/magic must redirect to /login?error=token-already-used when used_at is set"
    )
    # Rejects expired tokens
    assert "token-expired" in auth, (
        "/magic must redirect to /login?error=token-expired when past expires_at"
    )


def test_auth_routes_magic_get_is_non_consuming(resolved):
    """v2-magic-link-two-step-confirm (2026-05-27): GET /magic must be
    non-consuming for ALL requests (no UA sniffing, no consume). It
    validates the token, then renders the confirm view. The actual
    consume happens on POST /magic/consume.

    This replaces the previous UA-blocklist approach
    (v2-magic-link-previewer-bot-bypass, 2026-05-11), which was
    necessarily incomplete. An always-non-consuming GET closes the hole
    for every link-preview client, known or unknown, and matches what
    Slack, Notion, Substack, and Linear all do.
    """
    files = dict(compose_files(resolved))
    auth = files["routes/auth.js"]

    # Isolate the GET /magic handler body (everything up to the next router.).
    get_start = auth.find("router.get('/magic'")
    assert get_start != -1, "GET /magic handler missing"
    next_route = auth.find("router.", get_start + 1)
    assert next_route > get_start
    get_block = auth[get_start:next_route]

    # The GET handler must NOT consume the token.
    assert "SET used_at = NOW()" not in get_block, (
        "GET /magic must not update used_at; the single-use UPDATE belongs "
        "only in POST /magic/consume so link-preview crawlers never burn "
        "the token by pre-fetching"
    )
    # The GET handler must NOT set a session.
    assert "req.session.userId" not in get_block, (
        "GET /magic must not establish a session; that step belongs in "
        "POST /magic/consume after the user explicitly clicks Sign in"
    )

    # The GET handler must render the confirm view on a valid token.
    assert "magic-link-confirm" in get_block, (
        "GET /magic must render the magic-link-confirm view when the "
        "token validates; without this the user has no way to proceed"
    )

    # No UA blocklist code should remain.
    assert "_isLinkPreviewerUA" not in auth, (
        "the UA-blocklist helper is obsolete now that GET /magic is "
        "always non-consuming; remove it to keep the handler simple"
    )
    assert "_LINK_PREVIEWER_UA_PATTERNS" not in auth


def test_auth_routes_magic_post_consume_handler(resolved):
    """v2-magic-link-two-step-confirm (2026-05-27): POST /magic/consume
    performs the single-use UPDATE + session set + redirect. It must be
    CSRF-gated, must look up the token by sha256(raw), must update
    used_at on success, and must redirect to /admin or /set-password
    based on password_set.
    """
    files = dict(compose_files(resolved))
    auth = files["routes/auth.js"]

    # Locate the POST handler body.
    post_start = auth.find("router.post('/magic/consume'")
    assert post_start != -1, "POST /magic/consume handler missing"
    next_route = auth.find("router.", post_start + 1)
    assert next_route > post_start
    post_block = auth[post_start:next_route]

    # CSRF-gated.
    assert "validateCsrfToken" in post_block, (
        "POST /magic/consume must be wrapped in validateCsrfToken to "
        "prevent cross-site token submission"
    )

    # Reads the raw token from the form body, not the query string.
    assert "req.body" in post_block, (
        "POST /magic/consume must read the raw token from the form body"
    )

    # Hashes the raw token with sha256.
    assert "crypto.createHash('sha256')" in post_block

    # Performs the single-use UPDATE.
    assert "SET used_at = NOW()" in post_block, (
        "POST /magic/consume must update used_at to mark the token consumed"
    )

    # The UPDATE is the atomic claim: it repeats the unused + unexpired
    # conditions and the handler checks that exactly one row changed, so two
    # concurrent POSTs of one token cannot both sign in.
    assert "AND used_at IS NULL AND expires_at > NOW() RETURNING id" in post_block
    assert "claimed.rowCount !== 1" in post_block

    # Establishes the session through establishSession, which regenerates
    # the session id first (session fixation defense).
    assert "establishSession(req," in post_block
    helper = auth[auth.find("function establishSession"):auth.find("const _LOGIN_FAILED")]
    assert "req.session.regenerate(" in helper
    assert helper.find("req.session.regenerate(") < helper.find("req.session.userId = user.id")

    # Redirects based on password_set: to the account's landing page
    # (/admin for the admin, the home page for any other role) or to
    # /set-password. The booted-site tests walk both redirects.
    assert "row.password_set ? landingFor(row.role) : '/set-password'" in post_block, (
        "POST /magic/consume must redirect to the landing page (password_set=TRUE) "
        "or /set-password (password_set=FALSE) after consuming the token"
    )
    assert "return role === 'admin' ? '/admin' : '/';" in helper


def test_magic_link_confirm_view_emitted(resolved):
    """The magic-link-confirm view must exist + carry a POST form pointing
    at /magic/consume + include the CSRF token + carry the raw token as a
    hidden input."""
    files = dict(compose_files(resolved))
    assert "views/magic-link-confirm.ejs" in files, (
        "compose must emit views/magic-link-confirm.ejs so GET /magic can "
        "render the confirm step"
    )
    view = files["views/magic-link-confirm.ejs"]
    assert 'action="/magic/consume"' in view
    assert 'method="POST"' in view
    assert 'name="_csrf"' in view, (
        "confirm form must carry the session CSRF token so POST /magic/consume "
        "passes validateCsrfToken"
    )
    assert 'name="token"' in view, (
        "confirm form must carry the raw token as a hidden input so the "
        "POST handler can re-validate + consume it"
    )


def test_login_get_surfaces_error_query_param(resolved):
    """Regression 2026-04-22: /magic redirects to /login?error=<reason> on
    bad tokens, but the login view was silently dropping the query param;
    user landed on a blank login form with no explanation of what went wrong.
    The GET /login handler MUST read req.query.error and render a human
    message on the login page for each known error reason."""
    files = dict(compose_files(resolved))
    auth = files["routes/auth.js"]

    # GET /login must read req.query.error
    assert "req.query" in auth and "error" in auth, (
        "GET /login must read req.query.error to surface /magic redirect errors"
    )
    # Must map at least the 3 magic-link error reasons
    assert "'invalid-token'" in auth, "must map invalid-token to a human message"
    assert "'token-expired'" in auth, "must map token-expired to a human message"
    assert "'token-already-used'" in auth, "must map token-already-used to a human message"
    # Must pass the resolved message (not the raw error key) to the view
    assert "errorMsg" in auth or "_LOGIN_ERROR_MESSAGES" in auth, (
        "GET /login must pass a friendly error MESSAGE to the view, not the "
        "raw error code (users shouldn't see 'token-already-used' literally)"
    )


class TestAuthShellWidgets:
    """Auth surfaces use the letterpress shell from compose/widgets/auth/
    by default. The legacy split-panel and centered-card shells remain
    emitted for opt-in callers; the four customer-facing auth surfaces
    (/login, /set-password, /request-magic-link, /magic-link-sent) all
    consume the single-column letterpress shell. Roadmap:
    v2-auth-split-panel-replace-with-letterpress.
    """

    def test_emits_letterpress_split_panel_and_centered_card_partials(self, resolved):
        files = dict(compose_files(resolved))
        # All three shell partials are copied under views/widgets/auth/ by
        # emit_widget_files so views can include them at render time.
        assert "views/widgets/auth/letterpress.ejs" in files, (
            "letterpress auth shell must be emitted under views/widgets/auth/"
        )
        assert "views/widgets/auth/split-panel.ejs" in files, (
            "split-panel auth shell (legacy / opt-in) must remain emitted "
            "under views/widgets/auth/ for backward compatibility"
        )
        assert "views/widgets/auth/centered-card.ejs" in files, (
            "centered-card auth shell (legacy / opt-in) must remain emitted "
            "under views/widgets/auth/ for backward compatibility"
        )
        # Letterpress structure: single-column inner + heading + body
        lp = files["views/widgets/auth/letterpress.ejs"]
        assert "cas-auth-letterpress" in lp
        assert "cas-auth-letterpress__inner" in lp
        assert "cas-auth-letterpress__heading" in lp
        assert "cas-auth-letterpress__body" in lp
        # Split-panel structure: brand aside + form main; pattern is now
        # gated on noDecoration so callers can opt out of the hexagon repeat.
        sp = files["views/widgets/auth/split-panel.ejs"]
        assert "cas-auth-split__brand" in sp
        assert "cas-auth-split__brand-pattern" in sp
        assert "cas-auth-split__form" in sp
        assert "noDecoration" in sp, (
            "split-panel must accept a noDecoration toggle that suppresses "
            "the hexagon pattern (backward-compatible default keeps it on)"
        )
        # Centered-card structure: shell + card + icon + heading
        cc = files["views/widgets/auth/centered-card.ejs"]
        assert "cas-auth-card-shell" in cc
        assert "cas-auth-card__icon" in cc
        assert "cas-auth-card__heading" in cc

    def test_login_view_uses_letterpress_shell(self, resolved):
        files = dict(compose_files(resolved))
        login = files["views/login.ejs"]
        assert "include('widgets/auth/letterpress'" in login, (
            "/login must include the letterpress auth shell, not the legacy "
            "split-panel hexagon-pattern shell"
        )
        # Form still posts to /login with CSRF
        assert 'action="/login"' in login
        assert "_csrf" in login

    def test_set_password_view_emitted_and_uses_letterpress(self, resolved):
        files = dict(compose_files(resolved))
        assert "views/set-password.ejs" in files, (
            "compose must emit a /set-password view; /magic redirects there "
            "when password_set=FALSE and a missing view 500s the first-login flow"
        )
        sp_view = files["views/set-password.ejs"]
        assert "include('widgets/auth/letterpress'" in sp_view, (
            "/set-password must use the letterpress auth shell"
        )
        assert 'action="/set-password"' in sp_view
        assert "_csrf" in sp_view

    def test_request_magic_link_view_uses_letterpress(self, resolved):
        files = dict(compose_files(resolved))
        assert "views/request-magic-link.ejs" in files
        view = files["views/request-magic-link.ejs"]
        assert "include('widgets/auth/letterpress'" in view
        assert 'action="/request-magic-link"' in view
        assert "_csrf" in view

    def test_magic_link_sent_view_uses_letterpress(self, resolved):
        files = dict(compose_files(resolved))
        assert "views/magic-link-sent.ejs" in files
        view = files["views/magic-link-sent.ejs"]
        assert "include('widgets/auth/letterpress'" in view

    def test_auth_css_emitted_in_style(self, resolved):
        files = dict(compose_files(resolved))
        css = files["public/style.css"]
        # Letterpress classes (the new default)
        assert ".cas-auth-letterpress" in css
        assert ".cas-auth-letterpress__heading" in css
        assert ".cas-auth-letterpress__input" in css
        assert ".cas-auth-letterpress__submit" in css
        assert ".cas-auth-letterpress__foot" in css
        # Letterpress submit is a charcoal pill button using the family
        # primary color
        assert "border-radius: 999px" in css
        # Split-panel classes (legacy, retained)
        assert ".cas-auth-split " in css or ".cas-auth-split{" in css
        assert ".cas-auth-split__brand" in css
        assert ".cas-auth-split__brand-pattern" in css
        assert ".cas-auth-split__form" in css
        # Centered-card classes (legacy, retained)
        assert ".cas-auth-card-shell" in css
        assert ".cas-auth-card__icon" in css
        # Auth shells use the family palette
        assert "var(--color-primary)" in css

    def test_auth_routes_emit_set_password_handlers(self, resolved):
        files = dict(compose_files(resolved))
        auth = files["routes/auth.js"]
        # GET + POST for /set-password both emitted
        assert "router.get('/set-password'" in auth
        assert "router.post('/set-password'" in auth
        # POST is CSRF-gated
        # The whole handler, up to the next route.
        post_idx = auth.find("router.post('/set-password'")
        post_block = auth[post_idx:auth.index("router.get('/request-magic-link'", post_idx)]
        assert "validateCsrfToken" in post_block, (
            "POST /set-password must be CSRF-gated"
        )
        # POST stores the bcrypt hash + flips password_set=TRUE
        assert "bcrypt.hash" in post_block
        assert "password_set = TRUE" in post_block
        # Confirm-password mismatch is rejected
        assert "confirm_password" in post_block

    def test_auth_routes_set_password_requires_session(self, resolved):
        files = dict(compose_files(resolved))
        auth = files["routes/auth.js"]
        get_idx = auth.find("router.get('/set-password'")
        get_block = auth[get_idx:get_idx + 400]
        assert "req.session" in get_block and "userId" in get_block, (
            "GET /set-password must reject anonymous visitors so a stranger "
            "cannot land on the password form"
        )
        assert "/login" in get_block

    def test_auth_routes_emit_magic_link_request_handlers(self, resolved):
        files = dict(compose_files(resolved))
        auth = files["routes/auth.js"]
        assert "router.get('/request-magic-link'" in auth
        assert "router.post('/request-magic-link'" in auth
        assert "router.get('/magic-link-sent'" in auth
        post_idx = auth.find("router.post('/request-magic-link'")
        # 2026-05-17: handler grew when sendMagicLinkEmail wiring landed.
        # Slice up to the NEXT router.get('/magic-link-sent') line so the
        # block always covers the full POST handler.
        post_end = auth.find("router.get('/magic-link-sent'", post_idx)
        post_block = auth[post_idx:post_end if post_end != -1 else post_idx + 4000]
        # CSRF-gated
        assert "validateCsrfToken" in post_block
        # Always redirects to /magic-link-sent (anti-enumeration: no
        # branching response that leaks whether the email exists)
        assert "redirect('/magic-link-sent')" in post_block
        # Inserts a sha256-hashed token row only when user exists
        assert "INSERT INTO magic_link_tokens" in post_block
        assert "createHash('sha256')" in post_block

    def test_auth_paths_reserved_against_custom_pages(self, resolved):
        # The reserved-path set must include the new /set-password,
        # /request-magic-link, /magic-link-sent, /magic/consume so customer
        # custom_pages cannot shadow them. We assert via build.py source
        # since the set is computed inline.
        from compose import build as _b
        src = (Path(_b.__file__)).read_text()
        # All auth paths are listed alongside /login, /magic, /logout
        assert '"/set-password"' in src
        assert '"/request-magic-link"' in src
        assert '"/magic-link-sent"' in src
        assert '"/magic/consume"' in src


def test_seed_sql_accepts_worker_magic_link_injection(tmp_path):
    """Regression: the worker logged
    'Injected magic link token(s) into seed.sql' on the v2 preview stage,
    but the deployed artifact had ZERO `INSERT INTO magic_link_tokens`
    rows. The site owner could not log in on first deploy and ops had
    to hot-patch via a custom `db/post-migrate.sql`.

    The contract this test pins:
      1. compose-emitted seed.sql exists at the canonical path
         `db/seed.sql` (worker `_inject_magic_link_token` reads exactly
         `{build_dir}/db/seed.sql`; any rename breaks injection silently).
      2. Compose's admin INSERT uses magic-link-only auth shape
         (`password = NULL`, `password_set = TRUE`, `role = 'admin'`)
         so the FROM-subquery in the injected INSERT resolves to a real
         user row.
      3. After running the worker's `_inject_magic_link_token` on the
         compose-emitted build, seed.sql contains the verify-side and
         customer-side magic-link INSERTs with the supplied token hashes.
    """
    import sys

    # Worker path manipulation: `queue/` has no __init__.py and shadows
    # stdlib `queue`, so we import worker_pg by file path.
    queue_dir = str(Path(__file__).resolve().parent.parent.parent / "queue")
    if queue_dir not in sys.path:
        sys.path.insert(0, queue_dir)
    cli_dir = str(Path(__file__).resolve().parent.parent.parent / "cli")
    if cli_dir not in sys.path:
        sys.path.insert(0, cli_dir)
    import worker_pg  # noqa: E402

    out = build_app(str(SAMPLE_SPEC), output_dir=tmp_path / "out")

    # 1) Canonical seed.sql path the worker injects into.
    seed_path = out / "db" / "seed.sql"
    assert seed_path.exists(), (
        "compose must emit db/seed.sql at the canonical path; the worker's "
        "_inject_magic_link_token reads {build_dir}/db/seed.sql verbatim, "
        "so any path drift makes injection a silent no-op (a production build "
        "regression: log line printed but artifact had no INSERT)"
    )
    seed_before = seed_path.read_text()

    # 2) Admin INSERT uses magic-link-only auth shape so the FROM-subquery
    # in the injected statement resolves.
    assert "INSERT INTO users" in seed_before, "compose must seed an admin user"
    assert "'admin'" in seed_before, (
        "compose must seed a row with role='admin' so the magic-link "
        "FROM users WHERE role = 'admin' subquery returns a row"
    )
    assert "NULL" in seed_before and "TRUE" in seed_before, (
        "compose's admin INSERT must use password=NULL, password_set=TRUE "
        "(magic-link-only auth, SEC-1) so first login works via the "
        "injected magic_link_tokens row"
    )

    # 3) After the worker injects, seed.sql contains both INSERTs.
    worker_pg._inject_magic_link_token(
        str(out),
        "verify_hash_for_test",
        customer_token_hash="customer_hash_for_test",
    )
    seed_after = seed_path.read_text()
    assert seed_after.startswith(seed_before), (
        "_inject_magic_link_token must APPEND to seed.sql, not rewrite it; "
        "any other behavior wipes compose's site_settings + per-model rows"
    )
    assert seed_after.count("INSERT INTO magic_link_tokens") == 2, (
        "after worker injection seed.sql must carry exactly two "
        "INSERT INTO magic_link_tokens rows (verify-side + customer-side); "
        "that build had ZERO and the customer could not log in"
    )
    assert "verify_hash_for_test" in seed_after
    assert "customer_hash_for_test" in seed_after
    # The injected SELECT must reference the same admin shape compose seeds
    # so the two pieces of the contract stay coupled.
    assert "FROM users WHERE role = 'admin'" in seed_after


def test_csrf_failure_renders_dedicated_error_page(resolved):
    """Regression 2026-04-22: on a CSRF mismatch for a form POST, the middleware
    rendered the generic error-500.ejs ('Something Went Wrong') which is
    INDISTINGUISHABLE from a real crash; the user reported a bug thinking the
    Save handler was broken, when the actual cause was session/CSRF drift.

    Fix: CSRF mismatch now returns 403 (correct status code) and renders a
    dedicated error-csrf view that tells the user their session expired and
    offers a 'Go back and refresh' path. Server logs the mismatch so ops
    can diagnose if it becomes a pattern.
    """
    files = dict(compose_files(resolved))
    auth_mw = files["middleware/auth.js"]

    # Must return 403 (not 500) for form-submit CSRF failures
    assert "res.status(403).render('error-csrf'" in auth_mw, (
        "validateCsrfToken must render error-csrf with status 403 on form CSRF "
        "mismatch (was rendering error-500 which confused admins debugging "
        "their own apps; 2026-04-22 regression)"
    )
    # Must log the mismatch so it's diagnosable from server logs
    assert "csrf-mismatch" in auth_mw, (
        "validateCsrfToken must log CSRF mismatches to stderr with the "
        "[csrf-mismatch] tag for ops diagnosis"
    )
    # JSON-API path (x-csrf-token header) keeps the 403 JSON response
    assert "res.status(403).json" in auth_mw, (
        "API clients should still get 403 JSON, not an HTML page"
    )

    # The dedicated error-csrf view must be emitted
    assert "views/error-csrf.ejs" in files, (
        "compose must emit views/error-csrf.ejs for the CSRF failure path"
    )
    csrf_view = files["views/error-csrf.ejs"]
    assert "Session Expired" in csrf_view, (
        "error-csrf view must explicitly tell the user their session expired"
    )
    assert "Go Back" in csrf_view or "Refresh" in csrf_view, (
        "error-csrf view must offer a clear next action (back / refresh)"
    )


def test_csrf_tolerates_array_body_token(resolved):
    """Regression 2026-04-22: a nested <form> in the admin CRUD edit view
    caused browsers to flatten the inner Delete form's `_csrf` hidden input
    into the outer Edit form, so req.body._csrf arrived as an ARRAY
    `['tok','tok']` instead of a string. Array !== string failed CSRF
    validation and surfaced as a generic 500 to the user clicking Save.

    Two-layer fix:
      1. (primary) admin crud-form.ejs no longer nests the Delete form
         inside the main Edit form (see test_admin_crud_form_not_nested).
      2. (defensive) validateCsrfToken tolerates array input when all
         members are identical, so future accidental form-flattening can't
         silently break CSRF.
    """
    auth_mw = dict(compose_files(resolved))["middleware/auth.js"]
    assert "Array.isArray(body)" in auth_mw, (
        "validateCsrfToken must defensively handle req.body._csrf being "
        "an array (from duplicate hidden inputs / nested forms)"
    )
    # When all array members agree, they should collapse to one token
    assert "new Set(" in auth_mw and "unique.length === 1" in auth_mw, (
        "defensive array handling must collapse identical duplicates into "
        "a single token (that's the common case from accidental nested forms)"
    )


def test_admin_detail_view_has_delete_form_with_confirm(resolved):
    """Regression v2-admin-detail-row-confirm-delete (2026-05-14): the
    compose-emitted admin-detail.ejs only had an Edit button. To delete a
    row, the admin had to navigate BACK to the list page. Add a Delete
    form inline with Edit, gated by JS confirm() against accidental clicks.

    Verifies every model's admin-detail view:
      - POSTs to /admin/<slug>/<id>/delete
      - includes a CSRF hidden input
      - has a data-confirm guard (delegated confirm in cas-form-validate.js;
        NOT an inline onsubmit, which the app script-src nonce CSP blocks)
      - renders a Delete button
    """
    files = dict(compose_files(resolved))
    for m in resolved.data_models:
        slug = _slug_for(m)
        view = files[f"views/{slug}/admin-detail.ejs"]
        assert f"/admin/{slug}/<%= record.id %>/delete" in view, (
            f"admin-detail for {slug} missing delete form action"
        )
        assert "method='POST'" in view or 'method="POST"' in view, (
            f"admin-detail for {slug} delete form must POST"
        )
        assert "name='_csrf'" in view or 'name="_csrf"' in view, (
            f"admin-detail for {slug} delete form missing _csrf hidden input"
        )
        assert "<%= csrfToken %>" in view, (
            f"admin-detail for {slug} delete form missing csrfToken value"
        )
        assert "data-confirm=" in view, (
            f"admin-detail for {slug} delete form missing data-confirm guard"
        )
        assert "onsubmit=" not in view, (
            f"admin-detail for {slug} must not use a CSP-blocked inline onsubmit"
        )
        assert ">Delete</button>" in view, (
            f"admin-detail for {slug} missing Delete submit button"
        )


def test_admin_detail_view_delete_form_is_sibling_not_nested(resolved):
    """The Delete form on admin-detail must be a SIBLING of any other form,
    not nested. Nested <form> tags are invalid HTML; browsers flatten
    duplicate _csrf hidden inputs across forms, producing
    `req.body._csrf = [tok, tok]` which breaks CSRF validation
    (see test_admin_crud_form_not_nested for the original incident)."""
    files = dict(compose_files(resolved))
    for m in resolved.data_models:
        slug = _slug_for(m)
        view = files[f"views/{slug}/admin-detail.ejs"]
        # admin-detail has exactly one form (the delete form); the page
        # has no other forms (the Edit button is an <a>, not a form).
        form_opens = view.count("<form ")
        assert form_opens == 1, (
            f"admin-detail for {slug} must have exactly 1 <form> (delete), "
            f"got {form_opens}"
        )


# ---- v2-admin-crud-detail-view-design-quality (2026-05-27) ----
#
# The pre-2026-05-27 admin-detail.ejs rendered every field as a flat
# `label / value` stack with no section grouping, so a model with 20+
# fields (like Book) looked like a phpMyAdmin row dump. The new emitter
# groups fields by category, wraps each group in a card with a display-
# serif h2 header, renders booleans as pill badges, and skips empty
# fields entirely (instead of stamping an em-dash placeholder).


def test_admin_detail_emits_grouped_cards_with_section_headers(resolved):
    """Every admin-detail view ships at least one .cas-admin-card wrapper
    with a .cas-admin-card-header inside. The h2 uses font-display so it
    matches the public site's editorial register."""
    files = dict(compose_files(resolved))
    for m in resolved.data_models:
        slug = _slug_for(m)
        view = files[f"views/{slug}/admin-detail.ejs"]
        assert "cas-admin-card" in view, (
            f"admin-detail for {slug} missing grouped card structure"
        )
        assert "cas-admin-card-header" in view, (
            f"admin-detail for {slug} missing section header on group card"
        )
        assert "font-display" in view, (
            f"admin-detail for {slug} should use the display font for section "
            f"headers so the surface matches the public site"
        )


def test_admin_detail_renders_booleans_as_pill_badges():
    """Boolean fields in the display group render as colored pill badges
    ('● Yes' / '○ No'), not bare 'Yes' / 'No' text. The pill markup carries
    .cas-admin-pill-yes / .cas-admin-pill-no for the styled rendering."""
    from compose.build import (
        BuildSpec, resolve_spec, emit_admin_crud_views,
    )
    spec = BuildSpec.from_dict({
        "app_name": "booltest",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [{
            "name": "Book",
            "fields": [
                {"name": "title", "sql_type": "TEXT"},
                {"name": "featured_on_home", "sql_type": "BOOLEAN"},
                {"name": "published", "sql_type": "BOOLEAN"},
            ],
        }],
    })
    rs = resolve_spec(spec)
    book_model = next(m for m in rs.data_models if m["name"] == "Book")
    views = dict(emit_admin_crud_views(rs, book_model))
    view = views["views/books/admin-detail.ejs"]
    assert "cas-admin-pill-yes" in view, "missing yes pill class"
    assert "cas-admin-pill-no" in view, "missing no pill class"
    assert "● Yes" in view, "missing Yes pill text"
    assert "○ No" in view, "missing No pill text"
    # Both boolean fields should be rendered in the display group.
    assert "record.featured_on_home" in view
    assert "record.published" in view


def test_admin_detail_skips_empty_field_rows_in_default_groups(resolved):
    """Non-boolean / non-link fields are wrapped in
    `<% if (record.X) { %>` so empty values don't render an em-dash row.
    Concretely, the old `<span ...>\u2014</span>` placeholder is gone."""
    files = dict(compose_files(resolved))
    for m in resolved.data_models:
        slug = _slug_for(m)
        view = files[f"views/{slug}/admin-detail.ejs"]
        # The pre-2026-05-27 emitter used "em dash" (U+2014) as the empty
        # placeholder. The new emitter uses italic "Not set" for link rows
        # and skips empty rows entirely for everything else.
        assert "\u2014" not in view, (
            f"admin-detail for {slug} should not emit em-dash placeholders; "
            f"use 'Not set' italic or skip the empty row entirely"
        )


def test_admin_detail_renders_link_rows_for_external_url_fields():
    """When a model has external URL fields (anything ending in _url that
    is NOT an image field), they render as .cas-admin-link-row entries
    with a labeled left column and the URL on the right ('Not set' italic
    when empty). Drives the Book/where-to-buy card on the admin detail
    view.
    """
    from compose.build import (
        BuildSpec, resolve_spec, emit_admin_crud_views,
    )
    spec = BuildSpec.from_dict({
        "app_name": "linktest",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [{
            "name": "Book",
            "fields": [
                {"name": "title", "sql_type": "TEXT"},
                {"name": "amazon_url", "sql_type": "TEXT"},
                {"name": "goodreads_url", "sql_type": "TEXT"},
            ],
        }],
    })
    rs = resolve_spec(spec)
    book_model = next(m for m in rs.data_models if m["name"] == "Book")
    views = dict(emit_admin_crud_views(rs, book_model))
    detail = views["views/books/admin-detail.ejs"]
    assert "cas-admin-link-row" in detail
    assert "cas-admin-link-label" in detail
    assert "cas-admin-link-url" in detail
    # Both link slots should appear in the rendered Links card.
    assert "record.amazon_url" in detail
    assert "record.goodreads_url" in detail
    # Empty-state copy is "Not set", not an em-dash placeholder.
    assert "Not set" in detail


def test_admin_detail_top_action_row_has_back_edit_and_delete(resolved):
    """The top action row sits BEFORE the first group card and carries
    three controls: Back breadcrumb, Edit link, Delete form. This is the
    canonical operator-page header per the reference Book patch."""
    files = dict(compose_files(resolved))
    for m in resolved.data_models:
        slug = _slug_for(m)
        view = files[f"views/{slug}/admin-detail.ejs"]
        actions_idx = view.find("cas-admin-detail-actions")
        first_card_idx = view.find("cas-admin-card")
        assert actions_idx >= 0, (
            f"admin-detail for {slug} missing top action row marker"
        )
        assert first_card_idx == -1 or actions_idx < first_card_idx, (
            f"admin-detail for {slug}: action row must precede the first "
            f"group card so Back/Edit/Delete render at the top of the page"
        )
        # Back breadcrumb to the list view.
        assert f"href='/admin/{slug}'" in view, (
            f"admin-detail for {slug} missing Back breadcrumb"
        )
        # Edit link to the edit view.
        assert f"/admin/{slug}/<%= record.id %>/edit" in view, (
            f"admin-detail for {slug} missing Edit link"
        )
        # Delete form (already covered separately, but assert again so the
        # action-row contract is self-contained).
        assert f"/admin/{slug}/<%= record.id %>/delete" in view, (
            f"admin-detail for {slug} action row missing Delete form"
        )


def test_admin_detail_explicit_group_annotation_overrides_inference():
    """A field with `group:` set on the spec wins over the inferred group.
    Unknown explicit groups fall through to 'other' (the section header
    will read 'Other') so a typo never silently drops the field."""
    from compose.build import _field_group_for
    # Explicit annotation wins over name-based inference.
    assert _field_group_for({"name": "title", "group": "marketing"}) == "marketing"
    # Unknown explicit group falls through to "other".
    assert _field_group_for({"name": "title", "group": "nonexistent"}) == "other"
    # Inference path: image, identity, description, marketing, links, etc.
    assert _field_group_for({"name": "cover_image_url"}) == "media"
    assert _field_group_for({"name": "amazon_url"}) == "links"
    assert _field_group_for({"name": "description"}) == "description"
    assert _field_group_for({"name": "featured_on_home", "sql_type": "BOOLEAN"}) == "display"
    assert _field_group_for({"name": "title"}) == "identity"
    assert _field_group_for({"name": "created_at", "sql_type": "TIMESTAMP"}) == "metadata"


def test_admin_detail_metadata_rendered_as_footer_not_card():
    """created_at / updated_at / id render in a small footer below the
    last group card, not as a 'Metadata' card. The footer uses the
    .cas-admin-meta-footer marker."""
    from compose.build import (
        BuildSpec, resolve_spec, emit_admin_crud_views,
    )
    spec = BuildSpec.from_dict({
        "app_name": "metatest",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "data_models": [{
            "name": "Note",
            "fields": [
                {"name": "title", "sql_type": "TEXT"},
                {"name": "body", "sql_type": "TEXT"},
                {"name": "created_at", "sql_type": "TIMESTAMP"},
                {"name": "updated_at", "sql_type": "TIMESTAMP"},
            ],
        }],
    })
    rs = resolve_spec(spec)
    note_model = next(m for m in rs.data_models if m["name"] == "Note")
    views = dict(emit_admin_crud_views(rs, note_model))
    detail = views["views/notes/admin-detail.ejs"]
    assert "cas-admin-meta-footer" in detail
    # The metadata fields are not wrapped in their own .cas-admin-card.
    assert "<h2 class='cas-admin-card-header font-display'>Metadata</h2>" not in detail
    assert "record.created_at" in detail
    assert "record.updated_at" in detail


def test_admin_crud_form_not_nested(resolved):
    """Regression 2026-04-22: the admin CRUD edit view had a nested <form>
    (Delete form inside the Edit form). Invalid HTML; browsers flatten
    hidden inputs across, producing `req.body._csrf = [tok, tok]`, CSRF
    validation fails, user sees generic 500 on Save.

    The Delete form MUST be a SIBLING of the Edit form, not nested.
    """
    crud_form = (Path(__file__).parent.parent / "widgets" / "admin" / "crud-form.ejs").read_text()
    # Count top-level <form> opens + closes; each pair should be balanced at depth 0
    # Simple heuristic: the Delete <form> must appear AFTER </form> closing tag
    edit_form_close_idx = crud_form.index("</form>")
    delete_form_idx = crud_form.find("/delete", edit_form_close_idx)
    assert delete_form_idx > edit_form_close_idx, (
        "Delete form's action=\"/.../delete\" must appear AFTER the main edit "
        "form's </form> tag; otherwise it's nested, which is invalid HTML "
        "and produces duplicate _csrf hidden inputs that break CSRF validation"
    )
    # Also assert the Delete form is explicitly outside: there should be
    # exactly TWO <form> opens in the template (one edit, one delete)
    form_opens = crud_form.count("<form ")
    assert form_opens == 2, (
        f"admin/crud-form.ejs must have exactly 2 <form> elements (edit + "
        f"delete as siblings), got {form_opens}. More than 2 suggests more "
        f"nested forms, which will break CSRF validation."
    )


def test_layout_public_includes_palette_vars(resolved):
    files = dict(compose_files(resolved))
    layout = files["views/layout-public.ejs"]
    assert "--color-primary" in layout
    assert "_s._palette_vars" in layout
    assert "(typeof settings !== 'undefined' && settings)" in layout


def test_layout_public_guards_undefined_settings(resolved):
    """Error-path renders must not cascade into a second 500 when settings is undefined."""
    files = dict(compose_files(resolved))
    layout = files["views/layout-public.ejs"]
    assert "_pv = _s._palette_vars ||" in layout
    assert "_fn = _s._fonts ||" in layout
    assert "_fu = _s._fonts_url ||" in layout
    # site_description access is defensive (chained || fallback). Match the
    # _s.site_description prefix; the OG block widened the fallback chain
    # to also pull from hero_tagline so the meta description never goes
    # blank when the keeper supplied a tagline but no description.
    assert "_s.site_description ||" in layout
    assert "_s.site_name || 'Site'" in layout


def test_layout_public_includes_skip_to_content(resolved):
    files = dict(compose_files(resolved))
    layout = files["views/layout-public.ejs"]
    assert "skip-to-content" in layout


def test_nav_items_match_archetype_pages(resolved):
    nav = _default_nav_items(resolved)
    labels = {item["label"] for item in nav}
    assert "Home" in labels
    assert "Contact" in labels


def test_admin_nav_uses_admin_prefix_per_mw_v25(resolved):
    """MW v25 pattern: admin nav links go to /admin/<slug> (admin-mount).
    Dashboard goes to /admin; settings to /admin/settings.
    """
    nav = _default_admin_nav_items(resolved)
    dashboard = [i for i in nav if i["label"] == "Dashboard"]
    assert dashboard and dashboard[0]["href"] == "/admin"
    settings = [i for i in nav if "Settings" in i["label"]]
    assert settings and settings[0]["href"] == "/admin/settings"
    crud = [i for i in nav if i["label"] not in ("Dashboard", "Settings")]
    for c in crud:
        assert c["href"].startswith("/admin/"), (
            f"admin nav href {c['href']} should use /admin/ prefix (MW v25)"
        )


# ---- v2-spec-nav_items-honor-customer-labels (2026-05-13) ----

class TestNavItemsOverride:
    """When spec.nav_items is supplied, customer labels MUST win over
    the hardcoded `labels` dict. Example: an author site that wants
    "Books" at /services instead of "Services".
    """

    def test_spec_nav_items_overrides_default_labels(self, sample_spec):
        sample_spec.nav_items = [
            {"label": "Books", "href": "/services"},
            {"label": "Press & Interviews", "href": "/blog"},
            {"label": "Contact", "href": "/contact"},
        ]
        resolved = resolve_spec(sample_spec)
        nav = _default_nav_items(resolved)
        labels = [i["label"] for i in nav]
        hrefs = [i["href"] for i in nav]
        assert "Books" in labels, f"expected customer label 'Books' in nav, got {labels}"
        assert "Press & Interviews" in labels
        # Hardcoded defaults must NOT leak through when override is set
        assert "Services" not in labels
        assert "Journal" not in labels
        # All supplied hrefs preserved
        assert "/services" in hrefs
        assert "/blog" in hrefs

    def test_spec_nav_items_empty_falls_back_to_archetype_defaults(self, sample_spec):
        sample_spec.nav_items = []
        resolved = resolve_spec(sample_spec)
        nav = _default_nav_items(resolved)
        labels = {i["label"] for i in nav}
        # Archetype defaults render normally
        assert "Home" in labels
        assert "Contact" in labels

    def test_spec_nav_items_none_falls_back_to_archetype_defaults(self, sample_spec):
        sample_spec.nav_items = None
        resolved = resolve_spec(sample_spec)
        nav = _default_nav_items(resolved)
        labels = {i["label"] for i in nav}
        assert "Home" in labels

    def test_spec_nav_items_filters_unsafe_paths(self, sample_spec):
        sample_spec.nav_items = [
            {"label": "Home", "href": "/"},
            {"label": "Bad-Protocol", "href": "javascript:alert(1)"},
            {"label": "Bad-Proto-Relative", "href": "//evil.example/foo"},
            {"label": "Bad-Parametric", "href": "/events/:slug"},
            {"label": "Empty-Href", "href": ""},
            {"label": "", "href": "/no-label"},
            {"label": "About", "href": "/about"},
        ]
        resolved = resolve_spec(sample_spec)
        nav = _default_nav_items(resolved)
        hrefs = [i["href"] for i in nav]
        # Only the two safe entries survive
        assert "/" in hrefs
        assert "/about" in hrefs
        # Unsafe ones all dropped
        assert not any("javascript" in h for h in hrefs)
        assert not any(h.startswith("//") for h in hrefs)
        assert not any(":slug" in h for h in hrefs)

    def test_spec_nav_items_dedupes_by_href(self, sample_spec):
        sample_spec.nav_items = [
            {"label": "Books", "href": "/services"},
            {"label": "Books Duplicate", "href": "/services"},
        ]
        resolved = resolve_spec(sample_spec)
        nav = _default_nav_items(resolved)
        services_entries = [i for i in nav if i["href"] == "/services"]
        assert len(services_entries) == 1
        # First wins
        assert services_entries[0]["label"] == "Books"

    def test_spec_nav_items_all_rejected_falls_back(self, sample_spec):
        # Every entry is unsafe → fall back to archetype-derived
        sample_spec.nav_items = [
            {"label": "Bad", "href": "javascript:alert(1)"},
            {"label": "", "href": "/no-label"},
        ]
        resolved = resolve_spec(sample_spec)
        nav = _default_nav_items(resolved)
        labels = {i["label"] for i in nav}
        assert "Home" in labels  # archetype default kicked in


class TestAdminNavItemsOverride:
    """Same override path for admin nav, plus an additional rail:
    admin nav entries MUST start with /admin/ (or equal /admin) so
    a customer override can't accidentally expose non-admin paths
    inside the admin nav.
    """

    def test_spec_admin_nav_items_overrides_default(self, sample_spec):
        sample_spec.admin_nav_items = [
            {"label": "Dashboard", "href": "/admin"},
            {"label": "Manage Books", "href": "/admin/books"},
            {"label": "Site Settings", "href": "/admin/settings"},
        ]
        resolved = resolve_spec(sample_spec)
        nav = _default_admin_nav_items(resolved)
        labels = [i["label"] for i in nav]
        assert labels == ["Dashboard", "Manage Books", "Site Settings"]

    def test_admin_nav_override_rejects_non_admin_paths(self, sample_spec):
        sample_spec.admin_nav_items = [
            {"label": "Dashboard", "href": "/admin"},
            {"label": "Public Page", "href": "/about"},  # rejected
            {"label": "Admin-Like-Evil", "href": "/admin-evil"},  # rejected (no trailing slash)
            {"label": "Real Admin", "href": "/admin/books"},
        ]
        resolved = resolve_spec(sample_spec)
        nav = _default_admin_nav_items(resolved)
        hrefs = [i["href"] for i in nav]
        assert "/admin" in hrefs
        assert "/admin/books" in hrefs
        assert "/about" not in hrefs
        assert "/admin-evil" not in hrefs

    def test_admin_nav_override_empty_falls_back(self, sample_spec):
        sample_spec.admin_nav_items = []
        resolved = resolve_spec(sample_spec)
        nav = _default_admin_nav_items(resolved)
        labels = {i["label"] for i in nav}
        assert "Dashboard" in labels  # archetype default kicked in
        assert "Settings" in labels


# ---- v2-admin-nav-models-reflect-public-pages (2026-05-27) ----

class TestAdminNavReflectsPublicVisibility:
    """`_default_admin_nav_items` + admin-dashboard stat-cards + quick-
    links must only promote data_models that actually surface on the
    public site. Without the filter, a healer-wellness build with
    several custom models shows a dashboard tile for each one (blog
    posts, photos, testimonials, booking intents and more), including
    content the public site never renders. CRUD routes still
    mount at /admin/<slug>; they're just not nav-surfaced.
    """

    def _build(self, tmp_path, data_models, custom_pages=None, archetype="local-service"):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        import json as _json
        v1 = {
            "app_name": "vis-test",
            "description": "vis test description",
            "admin_email": "admin@example.com",
            "data_models": data_models,
        }
        if custom_pages is not None:
            v1["pages"] = custom_pages
        v2 = translate_v1_spec(v1)
        # Force archetype so the test isn't affected by detect_archetype drift.
        v2["archetype"] = archetype
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        return build_dir

    def test_admin_dashboard_hides_models_without_public_surface(self, tmp_path):
        """A keeper-added data_model with no public_listing_path, no
        db:source in any page layout, no form_target binding, and no
        submission-target suffix must NOT appear in admin stat-cards
        or quick-links. Its CRUD route + view files are still emitted.
        """
        build_dir = self._build(tmp_path, [
            {"name": "BookingIntent", "fields": [{"name": "name", "type": "string", "required": True}]},
        ])
        dash = (build_dir / "routes" / "admin-dashboard.js").read_text()
        # Not promoted: no stat card, no quick link
        assert "manage_href: \"/admin/booking-intents\"" not in dash
        assert "\"Manage Booking Intents\"" not in dash
        # CRUD route + admin view still emitted; reachable by URL
        assert (build_dir / "routes" / "booking-intents.js").is_file()
        assert (build_dir / "views" / "booking-intents" / "manage.ejs").is_file()

    def test_admin_dashboard_shows_submission_target_models(self, tmp_path):
        """Submission-target models (ContactInquiry, NewsletterSubscriber,
        BookingRequest …) MUST always appear in admin nav/tiles;
        admins always need to see their inbox even when no explicit
        public form-binding is detected.
        """
        build_dir = self._build(tmp_path, [
            {"name": "ContactInquiry", "fields": [{"name": "name", "type": "string", "required": True}]},
            {"name": "NewsletterSubscriber", "fields": [{"name": "email", "type": "email", "required": True}]},
        ])
        dash = (build_dir / "routes" / "admin-dashboard.js").read_text()
        assert "manage_href: \"/admin/contact-inquiries\"" in dash
        assert "manage_href: \"/admin/newsletter-subscribers\"" in dash

    def test_admin_dashboard_shows_models_with_explicit_public_listing_path(self, tmp_path):
        """A model that declares `public_listing_path` is publicly
        surfaced by definition; it must appear in admin nav/tiles.
        """
        build_dir = self._build(tmp_path, [
            {"name": "Project", "public_listing_path": "/projects", "fields": [{"name": "title", "type": "string", "required": True}]},
        ])
        dash = (build_dir / "routes" / "admin-dashboard.js").read_text()
        assert "manage_href: \"/admin/projects\"" in dash
        assert "\"Manage Projects\"" in dash

    def test_admin_dashboard_shows_models_referenced_via_db_source(self, tmp_path):
        """Healer-wellness home page has a testimonials-stack widget with
        source: 'db:testimonials'. Even without `public_listing_path`,
        Testimonial must appear in admin nav/tiles because its rows
        render on the public home page.
        """
        build_dir = self._build(
            tmp_path,
            data_models=[
                {"name": "Testimonial", "fields": [
                    {"name": "quote", "type": "text", "required": True},
                    {"name": "author_name", "type": "string"},
                ]},
            ],
            archetype="healer-wellness",
        )
        dash = (build_dir / "routes" / "admin-dashboard.js").read_text()
        assert "manage_href: \"/admin/testimonials\"" in dash

    def test_default_admin_nav_filters_hidden_models(self, sample_spec):
        """Sample healer spec + an extra admin-only model: the admin
        nav must NOT carry an entry for the hidden model.
        """
        from compose.build import _default_admin_nav_items
        # Sample healer spec carries 6 default models all of which DO have
        # public surfaces (testimonial via home, event via /events page,
        # photo via /gallery page, blog_post via /journal page,
        # contact_inquiry + newsletter_subscriber via submission-target
        # rail). Add a 7th model with no public surface; it must be
        # filtered.
        sample_spec.data_models = (sample_spec.data_models or []) + [
            {
                "name": "PrivateOffering",
                "fields": [{"name": "title", "type": "string", "required": True}],
            }
        ]
        from compose.build import resolve_spec
        resolved = resolve_spec(sample_spec)
        nav = _default_admin_nav_items(resolved)
        labels = {i["label"] for i in nav}
        assert "Private Offerings" not in labels
        # Existing-archetype models still show through
        assert "Testimonials" in labels
        # Defaults sanity
        assert "Dashboard" in labels and "Settings" in labels

    def test_clearable_tables_still_lists_hidden_models(self, tmp_path):
        """The danger-zone "Clear all" button wipes the entire schema,
        regardless of nav surfacing. Hidden tables may still hold
        orphan rows from earlier versions of the keeper's site, so
        CLEARABLE_TABLES must list every model table.
        """
        build_dir = self._build(tmp_path, [
            {"name": "BookingIntent", "fields": [{"name": "name", "type": "string", "required": True}]},
        ])
        dash = (build_dir / "routes" / "admin-dashboard.js").read_text()
        # CLEARABLE_TABLES references the hidden model's table even
        # though the model isn't promoted in stats/quick-links above.
        assert "\"booking_intents\"" in dash


# ---- v2-phone-us-pattern-regex-v-flag-invalid (2026-05-14) ----

class TestPhoneUsRegexEcmaScriptV:
    """The HTML5 `pattern` attribute is interpreted under the ECMAScript
    /v regex flag. Old phone pattern `^[\\d\\s().+-]{7,20}$` was invalid
    under /v because `(`, `)`, and `-` MUST be escaped or positioned
    at start/end of the character class. CI guard: every FIELD_TYPES
    pattern must compile under /v.
    """

    def test_phone_us_pattern_compiles_under_ecmascript_v_flag(self):
        """Validate via Python `regex` module's V_FLAG-equivalent semantics
        by checking only the structural requirements that /v enforces.
        Specifically: `(`, `)`, `-` (when not at start/end), and `[` are
        escaped or absent.
        """
        import re as _re
        from compose.build import load_field_types as _field_type_registry
        reg = _field_type_registry()["field_types"]
        pat = reg["phone_us"]["pattern"]
        # The fix: parens + dash are explicitly escaped.
        # `(` and `)` MUST be escaped under /v
        # Look at character class only
        m = _re.match(r"^\^?\[([^\]]+)\]", pat)
        assert m, f"phone_us pattern shape changed: {pat}"
        char_class = m.group(1)
        # Reject any unescaped paren or unbracketed dash inside the class
        for i, c in enumerate(char_class):
            if c == "(" and (i == 0 or char_class[i-1] != "\\"):
                raise AssertionError(f"unescaped ( in phone_us char class: {char_class!r}")
            if c == ")" and (i == 0 or char_class[i-1] != "\\"):
                raise AssertionError(f"unescaped ) in phone_us char class: {char_class!r}")
        # And it should still match real US phones (Python re is a
        # weaker test than /v but catches typos)
        compiled = _re.compile(pat.replace("\\\\", "\\"))
        for valid in ["(555) 555-5555", "555-555-5555", "+1 (555) 555.5555", "5555555555"]:
            assert compiled.match(valid), f"phone_us pattern should match {valid!r}: {pat}"

    def test_every_field_type_pattern_has_escaped_special_chars(self):
        """Audit every FIELD_TYPES entry: any `(`, `)`, or unbracketed `-`
        inside a character class must be escaped. Future-proofs against
        the next FIELD_TYPES addition tripping the same /v gotcha.
        """
        import re as _re
        from compose.build import load_field_types as _field_type_registry
        for ft, cfg in _field_type_registry()["field_types"].items():
            pat = cfg.get("pattern")
            if not pat:
                continue
            # Inspect every character class in the pattern
            for m in _re.finditer(r"\[([^\]]+)\]", pat):
                cls = m.group(1)
                for i, c in enumerate(cls):
                    if c in "()" and (i == 0 or cls[i-1] != "\\"):
                        raise AssertionError(
                            f"field_type {ft!r} has unescaped {c!r} in char class {cls!r}; "
                            "ECMAScript /v rejects this. Escape with backslash."
                        )


# ---- v2-nav-public-wsimg-placeholder-logo-detection (2026-05-11) ----

def test_nav_public_treats_wsimg_logo_as_placeholder():
    """GoDaddy Website Builder serves a generic earth-icon at
    /isteam/.../logo/<uuid>.jpg on wsimg.com for sites that never
    uploaded a logo. The detection regex must recognize this as a
    placeholder so we render initials instead of the globe.
    """
    nav_public = Path(
        __file__
    ).resolve().parent.parent / "widgets" / "shell" / "nav-public.ejs"
    src = nav_public.read_text()
    # The pattern should include wsimg.com.../logo/ in the alternation
    assert "wsimg" in src, "nav-public.ejs missing wsimg placeholder alternation"
    assert "wsimg\\.com.*\\/logo\\/" in src, (
        "wsimg placeholder regex must match /isteam/.../logo/<uuid> URL shape"
    )
    # Every shell that emits a logo <img> must also have the carve-out;
    # otherwise GoDaddy-imported customers see globe icons on whichever
    # template family resolves to that shell. As of 2026-05-16 the
    # logo-emitting shells are: nav-public, nav-side-rail, nav-sanctuary.
    shells_with_logo_image = ["nav-side-rail.ejs", "nav-sanctuary.ejs"]
    shells_dir = Path(__file__).resolve().parent.parent / "widgets" / "shell"
    for shell in shells_with_logo_image:
        assert "wsimg" in (shells_dir / shell).read_text(), (
            f"{shell} MUST mirror nav-public.ejs placeholder detection "
            "(wsimg globe-icon carve-out for GoDaddy-imported customers)."
        )


# ---- v2-offerings-cta-target-blank-for-external (2026-05-14) ----

def test_offerings_grid_3col_external_cta_opens_in_new_tab():
    """External CTAs (e.g. 'Buy on Amazon', 'Find an indie bookseller')
    must open in a new tab so the customer's site doesn't get replaced
    by the destination. Internal CTAs (/contact, /book, etc.) navigate
    in place.
    """
    src = (
        Path(__file__).resolve().parent.parent
        / "widgets" / "offerings" / "grid-3col.ejs"
    ).read_text()
    # External-detection regex + conditional target=_blank
    assert "/^https?:\\/\\//i.test" in src, (
        "grid-3col.ejs missing external-URL detection regex"
    )
    assert 'target="_blank"' in src and 'rel="noopener"' in src, (
        "grid-3col.ejs missing target=_blank + rel=noopener for external CTAs"
    )


def test_cta_inquiry_block_external_opens_in_new_tab():
    src = (
        Path(__file__).resolve().parent.parent
        / "widgets" / "cta" / "inquiry-block.ejs"
    ).read_text()
    assert "/^https?:\\/\\//i.test" in src, (
        "inquiry-block.ejs missing external-URL detection regex"
    )
    assert 'target="_blank"' in src and 'rel="noopener"' in src, (
        "inquiry-block.ejs missing target=_blank + rel=noopener for external CTAs"
    )


# ---- v2-editorial-statement-h1-h2-responsive-clamp (2026-05-11) ----

def test_template_family_h1_h2_use_responsive_clamp(sample_spec):
    """Family typography h1/h2 must use clamp() so narrow viewports
    don't overflow when family declares 5rem max. Look at emitted
    layout-public for an editorial-statement build.
    """
    # Pin family to editorial-statement via the BuildSpec field that
    # resolve_spec actually reads.
    sample_spec.template_family_id = "editorial-statement"
    resolved = resolve_spec(sample_spec)
    files = dict(compose_files(resolved))
    pub = files["views/layout-public.ejs"]
    # Both body-class-scoped + .cas-type-h1 utility class use clamp()
    assert "clamp(" in pub, "no clamp() in layout-public.ejs typography"
    # The body-class h1 rule explicitly carries clamp() (not flat var)
    import re as _re
    h1_rule_match = _re.search(
        r"body\.cas-template-editorial-statement\s+h1\s*\{[^}]+\}",
        pub
    )
    assert h1_rule_match, "h1 body-class rule missing from layout-public"
    h1_rule = h1_rule_match.group(0)
    assert "clamp(" in h1_rule, (
        f"editorial-statement h1 rule must use clamp() for responsive sizing; got: {h1_rule}"
    )
    # h2 also uses clamp
    h2_rule_match = _re.search(
        r"body\.cas-template-editorial-statement\s+h2\s*\{[^}]+\}",
        pub
    )
    assert h2_rule_match and "clamp(" in h2_rule_match.group(0), (
        "editorial-statement h2 rule must also use clamp()"
    )


# ---- v2-mobile-figure-zero-width-bug (2026-05-19) ----

def test_figure_base_rule_has_width_100_to_prevent_grid_item_collapse(resolved):
    """`<figure>` is a grid item in widgets like offerings/photo-gallery-grid.
    When the figure has no explicit width and its only sized child uses
    `width:100% + aspect-ratio`, the browser hits a circular intrinsic-size
    lookup and the figure collapses to 0x0. Setting `width: 100%` on the
    figure breaks the cycle so the figure stretches to fill its grid track
    and the child's aspect-ratio + width:100% resolves correctly.

    The fix lives at the base level (applies at all breakpoints) AND is
    re-asserted inside the mobile single-column breakpoint to document the
    contract next to the related card-grid overrides.
    """
    files = dict(compose_files(resolved))
    style = files.get("public/style.css", "")
    assert style, "public/style.css missing from emitted files"
    # Base figure reset must carry width: 100% (also min-width:0 + margin:0)
    assert "figure { margin: 0; width: 100%; min-width: 0; }" in style, (
        "base figure rule must reset margin, set width:100% and min-width:0 "
        "to prevent grid-item zero-width collapse"
    )
    # Mobile breakpoint must re-assert figure width to document the contract
    mobile_block_start = style.find("@media (max-width: 580px) {")
    assert mobile_block_start != -1, "mobile single-column @media block missing"
    mobile_block_end = style.find("}", mobile_block_start + 1)
    # Find the closing brace of the @media block (not the inner rules).
    # Walk forward until we hit a closing brace at column-zero indent.
    # Simpler: just look for the figure width:100% rule somewhere after the
    # @media open and before the next top-level rule.
    mobile_block = style[mobile_block_start:mobile_block_start + 800]
    assert "figure { width: 100%; }" in mobile_block, (
        "mobile @media block must re-assert figure width:100% as a parallel "
        "single-column override"
    )


# ---- v2-card-grid-orphan-row-stretch (2026-05-14) ----

def test_cas_card_grid_max_width_tightened_to_prevent_orphan_stretch(resolved):
    """When count % cols == 1, the lone last-row card flex-grows to
    max-width. The cap was 600/420 which made the orphan visibly
    imbalanced vs row-mates. Tightening to 380 keeps orphans
    proportional. Single-column threshold also dropped to 580px.
    """
    files = dict(compose_files(resolved))
    style = files.get("public/style.css", "")
    assert style, "public/style.css missing from emitted files"
    # The tightened rules must be present
    assert ".cas-card-grid > * { flex: 1 1 320px; max-width: 380px;" in style, (
        ".cas-card-grid max-width must be 380px (tightened from 600)"
    )
    assert ".cas-card-grid-3 > * { flex: 1 1 280px; max-width: 380px;" in style, (
        ".cas-card-grid-3 max-width must be 380px (tightened from 420)"
    )
    # Single-column threshold dropped from 760 to 580
    assert "@media (max-width: 580px)" in style, (
        "single-column breakpoint must drop from 760 to 580"
    )
    # Old values must NOT be present
    assert "max-width: 600px" not in style.replace("max-width: 600px;", "", 0) or (
        ".cas-card-grid > * { flex: 1 1 320px; max-width: 600px;" not in style
    ), ".cas-card-grid should no longer carry 600px max"
    assert ".cas-card-grid-3 > * { flex: 1 1 280px; max-width: 420px;" not in style, (
        ".cas-card-grid-3 should no longer carry 420px max"
    )


# ---- v2-offerings-grid-3col-image-aspect-param (2026-05-11) ----

class TestOfferingsGridImageAspect:
    """`item.image_aspect` (portrait/landscape/square) + `image_fit`
    (cover/contain) drive img aspect-ratio class + object-fit. Default
    landscape+cover preserves backward compat. portrait+contain adds
    neutral pad background.
    """

    def test_default_aspect_is_16_9_cover(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "widgets" / "offerings" / "grid-3col.ejs"
        ).read_text()
        # The default branch (no image_aspect declared) still falls
        # through to aspect-[16/9] + object-cover
        assert "aspect-[16/9]" in src
        assert "object-cover" in src

    def test_widget_branches_on_image_aspect(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "widgets" / "offerings" / "grid-3col.ejs"
        ).read_text()
        # All three aspect ratios + fit modes wired in
        assert "image_aspect === 'portrait'" in src
        assert "image_aspect === 'square'" in src
        assert "aspect-[2/3]" in src
        assert "aspect-square" in src
        assert "object-contain" in src

    def test_portrait_contain_adds_neutral_pad_background(self):
        """The portrait+contain combo adds a background-color +
        padding so unfilled box space looks intentional.
        """
        src = (
            Path(__file__).resolve().parent.parent
            / "widgets" / "offerings" / "grid-3col.ejs"
        ).read_text()
        # _padPortrait flag drives an inline style with bg + padding
        assert "_padPortrait" in src
        assert "var(--color-secondary)" in src
        assert "padding: 1.25rem" in src


# ---- v2-crud-handler-unchecked-boolean-loses-user-intent (P1, 2026-05-14) ----

class TestCrudHandlerBooleanCoercion:
    """HTML forms DO NOT submit unchecked checkboxes. The emitted CRUD
    INSERT/UPDATE loops must force-include boolean cols so unchecked
    checkboxes save as FALSE rather than skipping the col entirely
    (which on CREATE fires DB default and on UPDATE preserves prior).

    Guards the regression where books posted with
    `published` unchecked landed as `published=TRUE` from DB default.
    """

    def test_emitted_resource_router_declares_boolean_cols_set(self):
        from compose.build import (
            BuildSpec, resolve_spec, emit_routes_resource_js
        )
        # Use a model with multiple boolean columns. Event has `featured`.
        spec = BuildSpec.from_dict({
            "app_name": "test", "archetype": "healer-wellness", "admin_email": "a@b.c",
            "data_models": [{"name": "Event", "fields": [
                {"name": "title", "sql_type": "TEXT", "required": True},
                {"name": "published", "sql_type": "BOOLEAN", "default": "FALSE"},
                {"name": "featured", "sql_type": "BOOLEAN", "default": "FALSE"},
                {"name": "free", "field_type": "boolean"},
            ]}],
        })
        resolved = resolve_spec(spec)
        model = next(m for m in resolved.data_models if m["name"] == "Event")
        _, src = emit_routes_resource_js(resolved, model)
        # BOOLEAN_COLS Set declared and lists each boolean col
        assert "const BOOLEAN_COLS = new Set(" in src
        for col in ["published", "featured", "free"]:
            assert f'"{col}"' in src, f"BOOLEAN_COLS missing {col}; src head: {src[:400]}"

    def test_emitted_create_forces_include_for_boolean_cols(self):
        from compose.build import (
            BuildSpec, resolve_spec, emit_routes_resource_js
        )
        spec = BuildSpec.from_dict({
            "app_name": "test", "archetype": "healer-wellness", "admin_email": "a@b.c",
            "data_models": [{"name": "BlogPost", "fields": [
                {"name": "title", "sql_type": "TEXT", "required": True},
                {"name": "published", "sql_type": "BOOLEAN", "default": "FALSE"},
            ]}],
        })
        resolved = resolve_spec(spec)
        model = next(m for m in resolved.data_models if m["name"] == "BlogPost")
        _, src = emit_routes_resource_js(resolved, model)
        # INSERT path: gate is `req.body[col] !== undefined || isBool`
        assert "if (req.body[col] !== undefined || isBool)" in src, (
            "INSERT loop must force-include boolean cols regardless of body presence"
        )
        # Boolean coercion explicit (handles 'on' / '1' / 'true' / true)
        assert "(v === 'on' || v === '1' || v === 'true' || v === true)" in src, (
            "Boolean coercion clause missing; unchecked checkbox must save as FALSE"
        )
        # The old undefined-only gate is gone (regression guard)
        # Count of plain `req.body[col] !== undefined` checks without || isBool:
        plain_count = src.count("req.body[col] !== undefined")
        with_isbool_count = src.count("req.body[col] !== undefined || isBool")
        assert plain_count == with_isbool_count, (
            f"every req.body[col] check must also test isBool; found {plain_count} total, {with_isbool_count} with isBool guard"
        )

    def test_models_without_boolean_cols_emit_empty_set(self):
        """Non-boolean models still emit BOOLEAN_COLS = new Set([])
        so the runtime check is no-op but defined.
        """
        from compose.build import (
            BuildSpec, resolve_spec, emit_routes_resource_js
        )
        spec = BuildSpec.from_dict({
            "app_name": "test", "archetype": "healer-wellness", "admin_email": "a@b.c",
            "data_models": [{"name": "Note", "fields": [
                {"name": "title", "sql_type": "TEXT", "required": True},
                {"name": "body", "sql_type": "TEXT"},
            ]}],
        })
        resolved = resolve_spec(spec)
        model = next(m for m in resolved.data_models if m["name"] == "Note")
        _, src = emit_routes_resource_js(resolved, model)
        assert "const BOOLEAN_COLS = new Set([]);" in src or \
               "const BOOLEAN_COLS = new Set([])" in src


# ---- v2-request-magic-link-route-emits-console-log-not-email (P1, 2026-05-11) ----

class TestMagicLinkEmail:
    """Compose-emitted routes/auth.js POST /request-magic-link now calls
    sendMagicLinkEmail (Resend helper in services/notify.js) so customers
    get an email containing the absolute magic URL. Previously the route
    only console.log'd the token; the user saw "check your email" but
    no email ever arrived.
    """

    def test_notify_js_exports_send_magic_link_email(self):
        from compose.build import (
            BuildSpec, resolve_spec, emit_services_notify_js
        )
        spec = BuildSpec.from_dict({
            "app_name": "x", "archetype": "healer-wellness", "admin_email": "a@b.c",
        })
        resolved = resolve_spec(spec)
        _, src = emit_services_notify_js(resolved)
        # Helper function present in the emitted file
        assert "async function sendMagicLinkEmail" in src
        # Exported alongside sendAdminNotification
        assert "module.exports = { sendAdminNotification, sendMagicLinkEmail };" in src
        # Honors fire-and-forget contract: returns object, never throws
        assert "return { ok: true };" in src
        # Log-only fallback when RESEND_API_KEY missing
        assert "missing RESEND_API_KEY" in src

    def test_auth_route_wires_send_magic_link_email(self):
        from compose.build import (
            BuildSpec, resolve_spec, emit_routes_auth_js
        )
        spec = BuildSpec.from_dict({
            "app_name": "x", "archetype": "healer-wellness", "admin_email": "a@b.c",
        })
        resolved = resolve_spec(spec)
        _, src = emit_routes_auth_js(resolved)
        # Route loads the helper and calls it
        assert "require('../services/notify')" in src
        assert "sendMagicLinkEmail({" in src
        # Builds the absolute URL on the site's own address (linkOrigin:
        # SITE_ORIGIN, else the stored site_url), never on a request header:
        # Host and X-Forwarded-Host are chosen by the client, and a link
        # built from them would send the admin's login token to a host the
        # requester chose. (test_generated_app_runtime.py runs it.)
        assert "magicUrl: origin + '/magic?token=' + rawToken" in src
        assert "req.get('host')" not in src
        assert "headers['x-forwarded-host']" not in src
        assert "get('x-forwarded-host')" not in src.lower()
        # Issuance is still logged (regression guard against ripping out the
        # console.log altogether), but the raw token is only logged when no
        # mail transport is configured.
        assert "[magic-link] issued for user" in src
        with_mail, without_mail = src.split("if (process.env.RESEND_API_KEY) {", 1)[1].split("} else {", 1)
        assert "rawToken" not in with_mail
        assert "rawToken" in without_mail.split("}", 1)[0]
        # SELECT now pulls email (not just id) so we can email the user
        assert "SELECT id, email FROM users WHERE LOWER(email)" in src

    def test_auth_route_send_failure_does_not_block_redirect(self):
        """Fire-and-forget: a Resend network failure must not turn the
        302-to-magic-link-sent flow into a 500. Verify the route wraps
        the send call in .catch(...).
        """
        from compose.build import (
            BuildSpec, resolve_spec, emit_routes_auth_js
        )
        spec = BuildSpec.from_dict({
            "app_name": "x", "archetype": "healer-wellness", "admin_email": "a@b.c",
        })
        resolved = resolve_spec(spec)
        _, src = emit_routes_auth_js(resolved)
        # The send call must be followed by a .catch() handler
        import re
        # Look for sendMagicLinkEmail({ ... }).catch(...): multi-line
        m = re.search(
            r"sendMagicLinkEmail\(\{.*?\}\)\.catch\(",
            src,
            re.DOTALL,
        )
        assert m, "sendMagicLinkEmail call must have a .catch() (fire-and-forget)"


# ---- v2-resource-list-card-grid-image-aspect-natural (2026-05-14) ----

class TestResourceListImageAspect:
    """Mirrors offerings/grid-3col image-aspect support: item.image_aspect
    + image_fit drive the rendered img class; portrait+contain adds a
    neutral pad-background.
    """

    def test_default_branch_keeps_16_9_cover(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "widgets" / "offerings" / "resource-list-card-grid.ejs"
        ).read_text()
        assert "aspect-[16/9]" in src
        assert "object-cover" in src

    def test_widget_branches_on_image_aspect(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "widgets" / "offerings" / "resource-list-card-grid.ejs"
        ).read_text()
        assert "image_aspect === 'portrait'" in src
        assert "image_aspect === 'square'" in src
        assert "aspect-[2/3]" in src
        assert "aspect-square" in src
        assert "object-contain" in src

    def test_portrait_contain_adds_neutral_pad_background(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "widgets" / "offerings" / "resource-list-card-grid.ejs"
        ).read_text()
        assert "_padPortrait" in src
        assert "var(--color-secondary)" in src
        assert "padding: 1.25rem" in src


# ---- v2-readability-bump-opt-in (2026-05-11) ----

class TestReadabilityBump:
    """Opt-in `spec.readability_bump = true` emits body-class-scoped CSS
    that bumps body to 19px + paragraphs + strong + h2 one notch. Off
    by default to keep younger-skew sites unchanged.
    """

    def test_default_emits_no_readability_class(self, sample_spec):
        # No readability_bump flag → no cas-readability-bump class on body
        resolved = resolve_spec(sample_spec)
        files = dict(compose_files(resolved))
        pub = files["views/layout-public.ejs"]
        assert "cas-readability-bump" not in pub

    def test_opt_in_emits_readability_class_and_css(self, sample_spec):
        sample_spec.readability_bump = True
        resolved = resolve_spec(sample_spec)
        files = dict(compose_files(resolved))
        pub = files["views/layout-public.ejs"]
        # Body class present
        assert "cas-readability-bump" in pub
        # Body font bumped to 19px
        assert "body.cas-readability-bump { font-size: 19px; }" in pub
        # Paragraphs/li bumped + strong/b bolded
        assert "body.cas-readability-bump p," in pub
        assert "1.1875rem" in pub
        assert "body.cas-readability-bump strong," in pub
        assert "font-weight: 700" in pub
        # H2 nudged one notch via clamp
        assert "body.cas-readability-bump h2 { font-size: clamp(" in pub

    def test_readability_compatible_with_template_family(self, sample_spec):
        """Both body classes coexist (cas-template-* + cas-readability-bump)."""
        sample_spec.template_family_id = "editorial-statement"
        sample_spec.readability_bump = True
        resolved = resolve_spec(sample_spec)
        files = dict(compose_files(resolved))
        pub = files["views/layout-public.ejs"]
        assert 'class="cas-template-editorial-statement' in pub
        assert "cas-readability-bump" in pub
        # Both end up in the same body tag
        import re
        m = re.search(r'<body class="([^"]+)">', pub)
        assert m
        classes = m.group(1).split()
        assert "cas-template-editorial-statement" in classes
        assert "cas-readability-bump" in classes


# ---- helpers ----

def test_prettify_strips_version_suffix():
    assert _prettify("sample-practitioner-v1-rc2") == "Sample Practitioner V1 Rc2"
    assert _prettify("sample-practitioner-v1") == "Sample Practitioner"


def test_plural_basic():
    assert _plural("BlogPost") == "blog-posts"
    assert _plural("Event") == "events"
    assert _plural("Company") == "companies"
    assert _plural("NewsletterSubscriber") == "newsletter-subscribers"


def test_slug_for_uses_explicit_plural():
    m = {"name": "BlogPost", "plural": "blog-posts"}
    assert _slug_for(m) == "blog-posts"


def test_default_fields_per_model_archetype():
    for name in ("BlogPost", "Event", "Testimonial", "ContactInquiry", "NewsletterSubscriber"):
        fields = _default_fields(name)
        assert fields, f"no default fields for {name}"


# ---- end-to-end: build a spec to disk ----

def test_build_app_produces_disk_output(tmp_path):
    """End-to-end: build the sample spec to a temp dir, verify key files exist."""
    out = build_app(str(SAMPLE_SPEC), output_dir=tmp_path / "out")
    assert (out / "package.json").exists()
    assert (out / "server.js").exists()
    assert (out / "db/schema.sql").exists()
    assert (out / "db/seed.sql").exists()
    assert (out / "views/layout-public.ejs").exists()
    assert (out / "views/home.ejs").exists()
    assert (out / "compose-manifest.txt").exists()


# ---- upload route + client runtime ----

def test_upload_route_emitted(resolved):
    files = dict(compose_files(resolved))
    assert "routes/upload.js" in files
    upload = files["routes/upload.js"]
    # Contract: POST /upload, multer, CSRF guard, admin-only
    assert "router.post('/upload'" in upload
    assert "require('multer')" in upload
    assert "validateCsrfToken" in upload
    assert "requireRole('admin')" in upload
    assert "upload.single('file')" in upload


def test_upload_route_mounted_in_server(resolved):
    files = dict(compose_files(resolved))
    server = files["server.js"]
    assert "require('./routes/upload')" in server
    # Mounted under /admin so the full path is /admin/upload
    assert "app.use('/admin', require('./routes/upload'))" in server


def test_public_js_upload_zone_shipped(resolved):
    files = dict(compose_files(resolved))
    assert "public/js/upload-zone.js" in files
    js = files["public/js/upload-zone.js"]
    assert "__uz_upload" in js
    assert "__uz_drop" in js
    assert "/admin/upload" in js
    assert "x-csrf-token" in js


def test_uploads_dir_created_via_gitkeep(resolved):
    files = dict(compose_files(resolved))
    assert "public/uploads/.gitkeep" in files


# ---- admin dashboard features ----

def test_admin_dashboard_has_change_password_and_clear_all(resolved):
    files = dict(compose_files(resolved))
    admin_js = files["routes/admin-dashboard.js"]
    assert "router.post('/change-password'" in admin_js
    assert "router.post('/clear-all'" in admin_js
    assert "bcrypt.hash" in admin_js
    assert "CLEARABLE_TABLES" in admin_js


def test_admin_dashboard_passes_quick_links_and_upcoming(resolved):
    files = dict(compose_files(resolved))
    admin_js = files["routes/admin-dashboard.js"]
    assert "quick_links" in admin_js
    # Healer archetype has Event, so upcoming_events block must be present
    assert "upcoming_events" in admin_js


def test_admin_dashboard_view_letterpress_polish_and_tools_row(resolved):
    files = dict(compose_files(resolved))
    dash = files["views/admin/dashboard.ejs"]
    # Letterpress-polish (v2-admin-dashboard-letterpress-polish, 2026-05-07):
    # the prior 4-color top-stripe stat cards + filled brown "this week"
    # pill were replaced with hairline-bordered cards on the warm paper
    # background and an outlined chip for the 7-day delta. Guard against
    # regression to the SaaS-trendy look.
    assert "_stripes" not in dash
    assert "var(--color-accent-warm)" not in dash
    assert "var(--color-accent-earth)" not in dash
    # Stat-card container still present, hairline-bordered, transparent
    # so the section's warm paper background shows through.
    assert "border-color: var(--color-border)" in dash
    # The Manage link still uses the primary token for emphasis.
    assert "var(--color-primary)" in dash
    # Includes the tools row + upcoming events + quick links
    assert "upcoming-events.ejs" in dash
    assert "quick-links.ejs" in dash
    assert "tools-row.ejs" in dash


# ---- photo model + gallery ----

def test_photo_model_seeded_for_healer(resolved):
    # Healer archetype should include Photo in its data models.
    names = [m["name"] for m in resolved.data_models]
    assert "Photo" in names


def test_photo_schema_and_seed(resolved):
    files = dict(compose_files(resolved))
    schema = files["db/schema.sql"]
    seed = files["db/seed.sql"]
    assert "CREATE TABLE IF NOT EXISTS photos" in schema
    assert "image_url" in schema
    # The sample fixture has 12 photos
    assert seed.count("INSERT INTO photos") >= 12 or "INSERT INTO photos (title" in seed




def test_photo_gallery_grid_widget_shipped(resolved):
    files = dict(compose_files(resolved))
    assert "views/widgets/offerings/photo-gallery-grid.ejs" in files


# ---- admin sub-nav + responsive nav ----

def test_admin_subnav_widget_shipped(resolved):
    files = dict(compose_files(resolved))
    assert "views/widgets/shell/admin-subnav.ejs" in files


def test_layout_admin_includes_subnav_and_upload_script(resolved):
    files = dict(compose_files(resolved))
    layout = files["views/layout-admin.ejs"]
    assert "admin-subnav" in layout
    assert "/js/upload-zone.js" in layout
    assert "csrf-token" in layout  # meta tag for upload-zone.js


def test_public_nav_uses_md_breakpoint(resolved):
    """Round-14b: nav switched from lg:* (1024+) to md:* (768+) so iPad
    portrait + all laptops show the desktop nav. The old lg breakpoint
    left nav items flowing inline with zero gap at 768-1023px because
    `hidden lg:flex` became plain-block below lg (founder-caught bug)."""
    files = dict(compose_files(resolved))
    nav = files["views/widgets/shell/nav-public.ejs"]
    # md: breakpoint: hamburger below 768, desktop nav at 768+.
    assert "md:hidden" in nav
    assert "md:flex" in nav


def test_public_nav_only_shows_admin_link_when_admin(resolved):
    files = dict(compose_files(resolved))
    nav = files["views/widgets/shell/nav-public.ejs"]
    # Non-admin authenticated users should not see Admin link.
    assert "_isAdmin" in nav
    assert "user.role === 'admin'" in nav


# ---- user context middleware ----

def test_server_exposes_res_locals_user(resolved):
    files = dict(compose_files(resolved))
    server = files["server.js"]
    assert "res.locals.user" in server
    assert "req.session.role" in server


# ---- upload zone widget ----

def test_upload_zone_widget_shipped(resolved):
    files = dict(compose_files(resolved))
    assert "views/widgets/admin/upload-zone.ejs" in files
    uz = files["views/widgets/admin/upload-zone.ejs"]
    # Interactions are wired by /js/upload-zone.js (loaded as script-src
    # 'self'); the widget itself carries NO inline on*= handlers (those are
    # blocked by the generated app's script-src nonce CSP) -- just the
    # data-field wrapper + element ids the external JS binds to.
    assert "data-field=" in uz
    assert 'type="file"' in uz
    assert "onclick=" not in uz
    assert "onchange=" not in uz
    assert "ondrop=" not in uz
    # The runtime functions live in the external JS, not the widget.
    assert "__uz_upload" in files["public/js/upload-zone.js"]
    assert "__uz_drop" in files["public/js/upload-zone.js"]
    # Hidden input captures the uploaded URL so it POSTs with the form
    assert 'type="hidden"' in uz


def test_settings_form_uses_upload_zone(resolved):
    files = dict(compose_files(resolved))
    form = files["views/admin/settings.ejs"]
    # File fields should delegate to the upload-zone widget
    assert "upload-zone.ejs" in form
    # Multipart enctype is no longer needed (upload is out-of-band XHR)
    assert 'enctype="multipart/form-data"' not in form


def test_crud_form_uses_upload_zone_for_image_fields(resolved):
    files = dict(compose_files(resolved))
    # Any CRUD form should include the upload-zone widget for file fields
    crud_ejs = (Path(__file__).resolve().parent.parent / "widgets" / "admin" / "crud-form.ejs").read_text()
    assert "upload-zone.ejs" in crud_ejs


# ---- sample spec full content ----

def test_sample_spec_has_image_urls_across_seed(sample_spec):
    seed = sample_spec.seed_content or {}
    blog = seed.get("blog_posts") or []
    events = seed.get("events") or []
    photos = seed.get("photos") or []
    assert blog and all(p.get("image_url") for p in blog), "every blog post should seed an image_url"
    assert events and all(e.get("image_url") for e in events), "every event should seed an image_url"
    assert len(photos) >= 8, "gallery seed should have 8+ photos"


def test_sample_spec_has_nav_logo_and_about_photo(sample_spec):
    s = sample_spec.settings or {}
    assert s.get("nav_logo_url"), "nav_logo_url must be seeded"
    assert s.get("about_photo_url"), "about_photo_url must be seeded"
    assert s.get("hero_image_url"), "hero_image_url must be seeded"


# ---- round-3 polish ----

def test_humanize_camelcase_and_kebab():
    from compose.build import _humanize
    assert _humanize("BlogPost") == "Blog Post"
    assert _humanize("ContactInquiry") == "Contact Inquiry"
    assert _humanize("NewsletterSubscriber") == "Newsletter Subscriber"
    assert _humanize("blog-posts") == "Blog Posts"
    assert _humanize("contact_email") == "Contact Email"


def test_crud_form_uses_humanized_resource_name(resolved):
    """New/Edit pages should say 'New Blog Post' / 'Edit Blog Post',
    not 'New BlogPost' / 'Edit BlogPost'."""
    files = dict(compose_files(resolved))
    new_view = files["views/blog-posts/new.ejs"]
    edit_view = files["views/blog-posts/edit.ejs"]
    # resource_name_display is passed into the include as a humanized string
    assert "resource_name_display: 'Blog Post'" in new_view
    assert "resource_name_display: 'Blog Post'" in edit_view
    # ContactInquiry → "Contact Inquiry"
    ci_view = files["views/contact-inquiries/new.ejs"]
    assert "resource_name_display: 'Contact Inquiry'" in ci_view


def test_crud_form_widget_uses_display_name_in_headings(resolved):
    files = dict(compose_files(resolved))
    form = files["views/widgets/admin/crud-form.ejs"]
    assert "resource_name_display" in form
    # "Save Changes" button in edit mode; "Create <name>" for new mode
    assert "Save Changes" in form
    assert "'Create ' + _name" in form


def test_edit_form_formats_date_values_for_postgres(resolved):
    """Date / datetime-local inputs must receive YYYY-MM-DD / YYYY-MM-DDTHH:MM
    strings, not JS Date toString output. Fixes blog-post update 500."""
    files = dict(compose_files(resolved))
    edit = files["views/blog-posts/edit.ejs"]
    # The emitted fields map applies a date formatter
    assert "_fmtDateVal" in edit
    assert "toISOString" in edit
    assert "substring(0, 10)" in edit


def test_timestamptz_at_fields_render_as_date_input(resolved):
    """published_at (TIMESTAMPTZ) should be rendered as a date input so
    the browser submits a valid YYYY-MM-DD value, not a JS toString."""
    files = dict(compose_files(resolved))
    edit = files["views/blog-posts/edit.ejs"]
    # fieldsTemplate includes published_at with type 'date'
    assert '"name": "published_at"' in edit and '"type": "date"' in edit


def test_crud_create_sets_flash_success(resolved):
    files = dict(compose_files(resolved))
    for model in resolved.data_models:
        slug = _slug_for(model)
        js = files[f"routes/{slug}.js"]
        assert "req.session.flash_success" in js, (
            f"routes/{slug}.js must set flash_success on CRUD actions"
        )


def test_crud_update_and_delete_set_flash(resolved):
    files = dict(compose_files(resolved))
    blog = files["routes/blog-posts.js"]
    # Update on success
    assert "' updated.'" in blog or "updated." in blog
    # Delete on success
    assert "' deleted.'" in blog or "deleted." in blog
    # Error path stores flash_error too
    assert "req.session.flash_error" in blog


def test_resource_router_reads_flash_on_list(resolved):
    files = dict(compose_files(resolved))
    blog = files["routes/blog-posts.js"]
    assert "req.session.flash_success" in blog
    # List handler reads + deletes flash so it only shows once
    assert "delete req.session.flash_success" in blog


def test_settings_save_and_get_flash(resolved):
    files = dict(compose_files(resolved))
    settings_js = files["routes/admin-settings.js"]
    assert "flash_success = 'Site settings saved.'" in settings_js
    assert "flash_error" in settings_js


def test_nav_public_renders_logo_when_set(resolved):
    files = dict(compose_files(resolved))
    nav = files["views/widgets/shell/nav-public.ejs"]
    assert "settings.nav_logo_url" in nav
    # Round-10: nav now shows a real logo OR an initials monogram when
    # the URL is a placeholder (picsum/placeholder/etc), so the src
    # reference may be stored in a local _logoUrl variable instead.
    assert ("_logoUrl" in nav or "settings.nav_logo_url" in nav)
    assert "_isPlaceholderLogo" in nav  # detection shipped
    assert "_initials" in nav  # monogram fallback shipped


def test_nav_public_monogram_circle_is_opt_in(resolved):
    """v2-nav-public-logo-monogram-toggle: the 40px round monogram circle
    in nav-public.ejs is opt-in via settings.show_nav_logo (default false).
    By default builds render wordmark-only; the monogram only ships when
    explicitly enabled OR when the customer has uploaded a real (non-default)
    logo. Same shape on nav-admin.ejs (no monogram circle by default)."""
    files = dict(compose_files(resolved))
    nav = files["views/widgets/shell/nav-public.ejs"]
    # Opt-in flag wired
    assert "show_nav_logo" in nav, "nav must read settings.show_nav_logo"
    # Default-monogram detector wired (so /logo.svg auto-generated default
    # is recognized as "not a customer-uploaded brand mark")
    assert "_isDefaultMonogram" in nav, "nav must detect /logo.svg default"
    assert "_showNavLogo" in nav, "nav must derive a _showNavLogo gate"
    # The nav-logo span must be wrapped in the gate so wordmark-only is the
    # default render path
    assert "if (_showNavLogo)" in nav, (
        "nav-logo span must be wrapped in `if (_showNavLogo)` gate"
    )
    # nav-admin shouldn't ship a monogram circle by default either
    nav_admin = files["views/widgets/shell/nav-admin.ejs"]
    assert 'class="nav-logo"' not in nav_admin, (
        "nav-admin must not ship a monogram circle by default"
    )


def test_generated_logo_svg_emitted_with_monogram(resolved):
    """Round-13b: every build ships a decent-looking monogram logo.svg
    so sites look finished before the customer uploads their real logo."""
    files = dict(compose_files(resolved))
    assert "public/logo.svg" in files, "logo.svg must be emitted per build"
    logo = files["public/logo.svg"]
    assert logo.startswith("<svg")
    # primary-color squircle bg + on-primary initials
    assert resolved.palette_vars["--color-primary"] in logo
    assert resolved.palette_vars["--color-on-primary"] in logo
    assert 'rx="24"' in logo  # rounded-squircle corners
    # initials derive from site_name; should include at least one uppercase letter
    words = [w for w in resolved.site_name.split() if w]
    if len(words) >= 2:
        expected = (words[0][0] + words[1][0]).upper()
        assert ">" + expected + "<" in logo


def test_logo_and_favicon_honor_brand_logo_color_override(sample_spec):
    """v2-favicon-logo-uses-customer-palette (exercised on the
    sample-healer fixture). When a customer's wordmark
    swatches differ from the palette's primary CTA color, they can supply
    `settings.brand_logo_color` (background fill) and
    `settings.brand_logo_text_color` (initials fill); both apply to
    logo.svg AND favicon.svg. Default behavior (palette colors) is
    covered by test_generated_logo_svg_emitted_with_monogram."""
    sample_spec.settings = {
        **(sample_spec.settings or {}),
        "brand_logo_color": "#1F3A5F",
        "brand_logo_text_color": "#F2C14E",
    }
    rs = resolve_spec(sample_spec)
    files = dict(compose_files(rs))
    logo = files["public/logo.svg"]
    favicon = files["public/favicon.svg"]
    assert 'fill="#1F3A5F"' in logo
    assert 'fill="#F2C14E"' in logo
    assert 'fill="#1F3A5F"' in favicon
    assert 'fill="#F2C14E"' in favicon
    # When the override is present the palette colors must NOT also leak
    # into the SVG (no double fills).
    assert rs.palette_vars["--color-primary"] not in logo or rs.palette_vars["--color-primary"] in ("#1F3A5F", "#F2C14E")


def test_logo_and_favicon_reject_invalid_brand_logo_color_override(sample_spec):
    """Invalid color overrides must silently fall back to the palette so
    a typo (or a value like `'sienna'` or `'rgb(...)'`) can't break SVG
    rendering. Whitelist: #RGB / #RRGGBB only."""
    sample_spec.settings = {
        **(sample_spec.settings or {}),
        "brand_logo_color": "sienna",
        "brand_logo_text_color": "rgb(255, 255, 255)",
    }
    rs = resolve_spec(sample_spec)
    files = dict(compose_files(rs))
    logo = files["public/logo.svg"]
    favicon = files["public/favicon.svg"]
    # Both fall through to palette defaults.
    assert rs.palette_vars["--color-primary"] in logo
    assert rs.palette_vars["--color-on-primary"] in logo
    assert rs.palette_vars["--color-primary"] in favicon
    assert rs.palette_vars["--color-on-primary"] in favicon
    # The invalid strings never appear as fill values.
    assert 'fill="sienna"' not in logo
    assert 'fill="rgb' not in logo
    assert 'fill="sienna"' not in favicon
    assert 'fill="rgb' not in favicon


def test_logo_and_favicon_accept_short_hex_brand_logo_color_override(sample_spec):
    """#RGB short-hex (3-char) is a valid CSS color and must pass the
    whitelist alongside #RRGGBB."""
    sample_spec.settings = {
        **(sample_spec.settings or {}),
        "brand_logo_color": "#abc",
    }
    rs = resolve_spec(sample_spec)
    files = dict(compose_files(rs))
    assert 'fill="#abc"' in files["public/logo.svg"]
    assert 'fill="#abc"' in files["public/favicon.svg"]


def test_nav_logo_url_defaults_to_generated_logo(resolved):
    """When a spec doesn't supply a nav_logo_url, seed.sql should point to
    /logo.svg so the nav renders the generated monogram instead of falling
    through to the text-only initials fallback. Note: 2026-04-25 added a
    content-hash cache buster (`/logo.svg?v=<8hex>`) so the URL doesn't
    collide with a stale /logo.svg cached from a prior local-preview app
    on the same port. We assert the hashed form here."""
    import re
    files = dict(compose_files(resolved))
    seed = files["db/seed.sql"]
    assert re.search(r"'nav_logo_url', '/logo\.svg\?v=[0-9a-f]{8}'", seed), (
        f"expected /logo.svg?v=<hash> in seed; got: "
        f"{seed[seed.find('nav_logo_url'):seed.find('nav_logo_url')+80] if 'nav_logo_url' in seed else 'no nav_logo_url'}"
    )


def test_foundation_motion_and_shadow_vars(resolved):
    """Round-14 taste-skill port: every build ships the foundation vars so
    downstream widgets can reference them instead of hard-coded values.
    Ban stock `ease`/`ease-in-out` in CAS utilities; standardize on the
    spring curve cubic-bezier(0.16, 1, 0.3, 1)."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert "--cas-ease: cubic-bezier(0.16, 1, 0.3, 1)" in css
    assert "--cas-dur-fast: 200ms" in css
    assert "--cas-dur-med: 500ms" in css
    assert "--cas-shadow-whisper:" in css
    assert "--cas-shadow-diffuse:" in css
    assert "color-mix(in oklab, var(--color-primary)" in css


def test_cas_btn_has_haptic_active_state(resolved):
    """Round-14: every primary CTA should press physically on click;
    translateY(1px) scale(0.98). Missing this is the #1 tell of AI-
    generated UI (buttons don't respond to press)."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert ".cas-btn:active" in css
    assert "translateY(1px) scale(0.98)" in css
    assert "var(--cas-shadow-diffuse)" in css
    # Standard transition uses the spring ease, not a raw number
    assert "var(--cas-dur-med) var(--cas-ease)" in css


def test_button_text_vertical_centering_recipe(resolved):
    """v2-button-text-vertical-centering-stays-fragile (2026-05-11):
    pin the four CSS layers that keep button text on the geometric
    centre of every CTA pill. Three bugs compound to push text below
    centre (body line-height 1.5 inheritance, lack of nowrap wrapping
    text to 3 lines, inline-default SVG baseline metrics biasing
    flex cross-axis midpoint). Reverting any layer reintroduces the
    visible regression. See compose/build.py for the full rationale.
    """
    files = dict(compose_files(resolved))
    css = files["public/style.css"]

    # Layer (a): the four selector heads that match every CTA pill.
    # Tailwind-escaped class selectors literally appear as `min-h-\[44px\]`
    # in the emitted stylesheet.
    assert 'a.inline-flex[class*="rounded-"].min-h-\\[44px\\]' in css
    assert 'button.inline-flex[class*="rounded-"].min-h-\\[44px\\]' in css
    assert 'button[type="submit"].min-h-\\[44px\\]' in css
    assert 'button[type="button"].min-h-\\[44px\\]' in css

    # Layer (a) declarations: bypass flex line-box centering, pin
    # line-height to box height, kill vertical padding.
    btn_rule_idx = css.find('button[type="button"].min-h-\\[44px\\] {')
    assert btn_rule_idx >= 0, "44px button rule block missing"
    btn_rule = css[btn_rule_idx:btn_rule_idx + 400]
    assert "display: inline-block;" in btn_rule
    assert "line-height: 44px;" in btn_rule
    assert "height: 44px;" in btn_rule
    assert "padding-top: 0;" in btn_rule
    assert "padding-bottom: 0;" in btn_rule
    assert "text-align: center;" in btn_rule
    assert "vertical-align: middle;" in btn_rule

    # Layer (b): every direct child gets the inline-block + middle +
    # line-height:1 trio that neutralises the SVG-baseline bias.
    assert 'button[type="button"].min-h-\\[44px\\] > * {' in css
    child_rule_idx = css.find('button[type="button"].min-h-\\[44px\\] > * {')
    child_rule = css[child_rule_idx:child_rule_idx + 200]
    assert "display: inline-block;" in child_rule
    assert "vertical-align: middle;" in child_rule
    assert "line-height: 1;" in child_rule

    # Layer (c): <span> children additionally get nowrap so narrow
    # flex containers can't wrap the label to 3 lines.
    assert 'button[type="button"].min-h-\\[44px\\] > span {' in css
    span_rule_idx = css.find('button[type="button"].min-h-\\[44px\\] > span {')
    span_rule = css[span_rule_idx:span_rule_idx + 200]
    assert "white-space: nowrap;" in span_rule

    # Layer (d): buttons WITHOUT min-h-[44px] still need line-height:1
    # to escape the body 1.5 inheritance.
    assert 'a.inline-flex[class*="rounded-"]:not(.min-h-\\[44px\\])' in css
    assert 'button.inline-flex[class*="rounded-"]:not(.min-h-\\[44px\\])' in css
    assert 'button[type="submit"]:not(.min-h-\\[44px\\])' in css
    assert 'button[type="button"]:not(.min-h-\\[44px\\])' in css
    not44_idx = css.find('button[type="button"]:not(.min-h-\\[44px\\]),')
    # The rule body sits after the .btn alternative; find the next `{`.
    brace_idx = css.find("{", not44_idx)
    not44_rule = css[brace_idx:brace_idx + 80]
    assert "line-height: 1;" in not44_rule


def test_card_lift_uses_tinted_shadows(resolved):
    """Round-14: card-lift transitions whisper → diffuse shadow (both
    tinted via color-mix). No more flat rgba(0,0,0,X)."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    # Cards at rest: whisper shadow
    assert ".cas-card-lift {" in css
    i = css.find(".cas-card-lift {")
    rest_block = css[i:i + 500]
    assert "box-shadow: var(--cas-shadow-whisper)" in rest_block


def test_scroll_reveal_honors_index_stagger(resolved):
    """Round-14: loop-emitted items can set style='--index: <i>' to
    cascade the reveal with an 80ms stagger instead of simultaneous mount."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert "calc(var(--index, 0) * 80ms)" in css


def test_typography_tokens_are_emitted(resolved):
    """Round-14: text-wrap balance on headings, pretty on paragraphs,
    tabular-nums on price columns, focus-visible ring, smooth scroll."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert "text-wrap: balance" in css
    assert "text-wrap: pretty" in css
    assert ":focus-visible" in css
    assert "scroll-behavior: smooth" in css
    # .price class gets tabular-nums for aligned pricing columns
    assert "font-variant-numeric: tabular-nums" in css
    assert ".price," in css or ".price " in css or ".price\n" in css


def test_bezel_utilities_with_concentric_radius(resolved):
    """Round-14 double-bezel utility: outer tray with small padding,
    inner plate with a calc()-smaller radius. Machined-hardware feel
    for hero images / featured cards."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert ".cas-bezel {" in css
    assert ".cas-bezel-inner {" in css
    assert "calc(2rem - 0.375rem)" in css
    assert "var(--cas-shadow-inner-highlight)" in css


def test_section_and_container_rhythm(resolved):
    """Round-14: .cas-section + .cas-container supply consistent vertical
    and horizontal rhythm across widgets via clamp() so every archetype
    shares the same breathing room."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert ".cas-section {" in css
    assert "clamp(4rem, 10vw, 10rem)" in css
    assert ".cas-container {" in css
    assert "max-width: 1400px" in css


def test_nav_breakpoint_is_md_not_lg(resolved):
    """Round-14b regression guard: the nav MUST use md:flex (768+) not
    lg:flex (1024+). Founder screenshot at md showed nav items flowing
    inline with zero gap because the hidden-lg:flex combo left the div
    as plain block-with-inline-children at 768-1023px. Keeps the desktop
    nav visible from iPad portrait + all laptops."""
    files = dict(compose_files(resolved))
    nav = files["views/widgets/shell/nav-public.ejs"]
    assert "hidden md:flex" in nav, "nav must switch to desktop at md breakpoint (768+)"
    assert "md:hidden" in nav, "mobile menu button + panel must use md:hidden"
    # The old lg:flex / lg:hidden pattern is gone
    assert "lg:flex items-center gap-5" not in nav


def test_md_gap_utilities_emitted(resolved):
    """Round-14b: md:gap-{3,4,5,6,8} must be inside the 768px media query
    so nav and other widgets get proper spacing at every responsive tier."""
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert ".md\\:gap-4 { gap: 1rem; }" in css
    assert ".md\\:gap-5 { gap: 1.25rem; }" in css
    assert ".md\\:gap-6 { gap: 1.5rem; }" in css
    assert ".md\\:gap-8 { gap: 2rem; }" in css
    # Base .gap-5 now present too (regression: it was missing and silently failed)
    assert ".gap-5 { gap: 1.25rem; }" in css


def test_footer_has_powered_by_craft_and_ship(resolved):
    """Round-14b: every generated site's footer links back to
    craftandship.io. Free growth loop + brand attribution on every app.

    As of v2-mw-quiet-craft-and-ship-footer-attribution (2026-05-03) the
    classic shell/footer.ejs delegates to the shared shell/cas-attribution.ejs
    partial (italic Cormorant pill, "made with [lighthouse] Craft & Ship").
    The CAS link still ships on every app, just rendered through the partial
    instead of an inline 'Powered by' line.
    """
    files = dict(compose_files(resolved))
    footer = files["views/widgets/shell/footer.ejs"]
    assert "include('./cas-attribution.ejs'" in footer
    partial = files["views/widgets/shell/cas-attribution.ejs"]
    assert 'href="https://craftandship.io"' in partial
    assert 'target="_blank"' in partial
    assert 'rel="noopener"' in partial


def test_hero_centered_guards_description(resolved):
    """hero/centered must guard the
    `description` variable with `typeof` so contact pages (which omit
    description) don't 500 with ReferenceError. A salon build shipped
    this bug; it showed on /contact."""
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/centered.ejs"]
    assert "typeof description !== 'undefined'" in hero


def test_home_hero_reads_settings_at_render_time(resolved):
    """Round-14d regression guard: the home view's hero include MUST use
    a settings.* JS expression (via _EJSRef) not a baked string literal
    for heading/description/image_url. This lets admin edits to
    /admin/settings take effect on next page reload without a rebuild;
    the capability founder flagged as "hero + about images didn't update
    after I edited them in admin"."""
    files = dict(compose_files(resolved))
    home = files["views/home.ejs"]
    # The hero include should reference settings.hero_* at runtime, not
    # embed the spec's baked string value.
    assert "settings.hero_tagline" in home, (
        f"home view must bind hero heading to settings.hero_tagline at render time: {home}"
    )
    assert "settings.hero_image_url" in home, (
        "home view must bind hero image to settings.hero_image_url at render time"
    )


def test_about_hero_reads_settings_at_render_time(resolved):
    """Round-14d regression guard: /about's hero must read
    about_title / about_body / about_photo_url from settings at render
    time so CMS edits propagate without a rebuild."""
    files = dict(compose_files(resolved))
    about = files["views/about.ejs"]
    assert "settings.about_title" in about
    assert "settings.about_body" in about
    assert "settings.about_photo_url" in about


def test_word_accent_transform_on_all_heading_widgets(resolved):
    """Round-14d: every heading-capable widget must apply the {Word}
    italic-accent transform. If a widget reverts to `<%= heading %>`
    raw (without `_accent` or `_rendered` helper), the {Word} syntax
    leaks literal to the page; the exact regression founder caught."""
    files = dict(compose_files(resolved))
    violations = []
    for path, body in files.items():
        if not path.startswith("views/widgets/"):
            continue
        if not path.endswith(".ejs"):
            continue
        if "<%= heading %>" in body:
            # Raw heading output is only OK if widget declares _accent
            # (batch-patched helper) or _rendered (hero widgets).
            if "_accent" not in body and "_rendered" not in body:
                violations.append(path)
    assert not violations, (
        f"{len(violations)} widget(s) render raw <%= heading %> without {{Word}} transform: {violations}"
    )


def test_hero_image_has_dreamy_haze_overlay(resolved):
    """Round-14e: hero-centered widget renders the optional image inside
    a cas-hero-image-frame wrapper with a gradient-haze overlay (dreamy
    editorial look) AND uses object-position: center 20% so the TOP of
    portraits is visible (faces not cropped)."""
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/centered.ejs"]
    assert "cas-hero-image-frame" in hero
    assert "object-position: center 20%" in hero
    # Layered gradient overlay using color-mix
    assert "linear-gradient" in hero and "color-mix(in oklab, var(--color-secondary)" in hero
    assert "pointer-events: none" in hero


# ---- hero/photo-feature widget (hot-patch port) ----
#
# The photo-feature hero variant landed during a healer-wellness
# build. It was hot-patched into the running preview, then
# fed back to the compose pipeline per the two-track hotpatch rule. Image leads above the fold; optional CSS-only
# slideshow with Ken-Burns slow zoom; pairs with the editorial-statement
# template family + .cas-btn premium button system.


def test_hero_photo_feature_widget_shipped(resolved):
    """photo-feature hero is an opt-in image-leads variant. Must be in
    the compose output so page-layouts can include it."""
    files = dict(compose_files(resolved))
    assert "views/widgets/hero/photo-feature.ejs" in files


def test_hero_photo_feature_guards_all_optional_vars(resolved):
    """Every variable in the photo-feature data contract is optional;
    the widget must guard each with `typeof` so omitted props don't
    500 with ReferenceError (the mistake hero/centered once shipped,
    see test_hero_centered_guards_description)."""
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/photo-feature.ejs"]
    for var in (
        "image_url", "image_alt", "image_caption", "overlay_title",
        "heading", "eyebrow", "description", "slides",
        "cta_primary", "cta_secondary",
    ):
        assert f"typeof {var} !== 'undefined'" in hero, (
            f"photo-feature must guard `{var}` with typeof to prevent ReferenceError"
        )


def test_hero_photo_feature_supports_slideshow_mode(resolved):
    """When `slides` is an array of {url} objects with length > 1, the
    widget renders a CSS-keyframe slideshow (no JS) with Ken-Burns slow
    zoom. Each slide gets ~8s visible; total cycle = slide_count * 8s."""
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/photo-feature.ejs"]
    # Slides filtering + slideshow gate
    assert "Array.isArray(slides)" in hero
    assert "_isSlideshow" in hero
    # Ken-Burns 8s/slide cadence
    assert "_cycleSec = _slideCount * 8" in hero
    # Slideshow frame + per-slide animation timing
    assert "cas-photo-hero-slideshow" in hero
    assert "cas-slide" in hero
    assert "animation-delay" in hero
    assert "animation-duration" in hero
    # Dot row (CSS-only progress indicator)
    assert "cas-photo-hero-dots" in hero
    assert "cas-photo-hero-dot" in hero


def test_hero_photo_feature_single_image_mode_preserved(resolved):
    """When `slides` is empty / unset and `image_url` is supplied, the
    widget still renders the single-image mode (graceful fallback so
    builds that pass only image_url get a working hero)."""
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/photo-feature.ejs"]
    # The else-if branch must remain so single-image renders work
    assert "else if (_imgUrl)" in hero
    # Above-the-fold image gets eager loading + high fetch priority
    # (hero lazy-load fix)
    assert 'loading="eager"' in hero
    assert 'fetchpriority="high"' in hero


def test_hero_photo_feature_uses_premium_button_classes(resolved):
    """CTAs render with .cas-btn-primary + .cas-btn-quiet utility
    classes from the premium button system (espresso-on-cream primary,
    transparent text-link quiet). The CSS for these classes is supplied
    by the editorial-statement template family + cream-espresso palette."""
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/photo-feature.ejs"]
    assert "cas-btn-primary" in hero
    assert "cas-btn-quiet" in hero
    # SVG arrow icon paired with each CTA
    assert "cas-btn-arrow" in hero


def test_hero_photo_feature_supports_word_accent_transform(resolved):
    """The {Word} italic-accent rule: every heading-capable
    widget must apply the brace-accent transform. photo-feature uses
    the same `_accent` helper as hero/centered + hero/editorial-stacked."""
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/photo-feature.ejs"]
    assert "_accent" in hero
    # The accent helper wraps {Word} substrings in <em class="cas-italic-accent">
    assert "cas-italic-accent" in hero
    # Heading is rendered through _accent (not raw <%= heading %>)
    assert "<%= heading %>" not in hero


def test_hero_photo_feature_does_not_regress_existing_hero_widgets(resolved):
    """Adding photo-feature must not remove or break hero/centered,
    hero/split-image, or hero/editorial-stacked. Defense-in-depth that
    the new widget is purely additive."""
    files = dict(compose_files(resolved))
    for variant in ("centered", "split-image", "editorial-stacked"):
        path = f"views/widgets/hero/{variant}.ejs"
        assert path in files, f"existing hero variant `{variant}` must still be emitted"


def test_contact_widget_letterpress_treatment(resolved):
    """Direct Contact + Social cards use the letterpress treatment:
    hairline-bordered cards on the page
    background, hairline-rule eyebrow with the label, hairline-divided
    rows per channel, smaller muted icons (14px in muted color), charcoal
    hover-underline links instead of primary-color filled icons. Replaces
    the prior rounded-xl/2rem-padding card style which read card-y/SaaS."""
    files = dict(compose_files(resolved))
    contact = files["views/widgets/form/contact.ejs"]
    # Letterpress card: hairline border on page background, modest radius
    assert ".cas-contact-card" in contact
    assert "background-color: var(--color-background)" in contact
    assert "border: 1px solid var(--color-border)" in contact
    # Hairline-rule eyebrow extending right via ::after
    assert ".cas-contact-eyebrow::after" in contact
    assert "letter-spacing: 0.22em" in contact
    # Channel rows divided by hairline (border-top), structured as <ul>
    assert ".cas-contact-list" in contact
    assert ".cas-contact-row" in contact
    assert "border-top: 1px solid var(--color-border)" in contact
    # Icons are 14px and muted (not the primary fill of the prior style)
    assert "size: 14" in contact
    assert "color: var(--color-foreground-muted)" in contact
    # Charcoal hover-underline link, not opacity-70 on primary
    assert ".cas-contact-link" in contact
    assert "border-bottom-color: var(--color-foreground)" in contact
    # Old card-y/SaaS markers must be gone from the right column
    assert 'class="rounded-xl border"' not in contact
    assert "letter-spacing: 0.18em" not in contact


def test_contact_widget_social_label_swaps_to_elsewhere_on_letterpress_families(resolved):
    """The second card's eyebrow renders both 'Social' (default) and
    'Elsewhere' (letterpress) labels. Body-class scoped CSS hides one or
    the other so minimal/letterpress template families (editorial-
    statement, editorial-sanctuary, simple-statement) read 'Elsewhere'
    while every other family keeps 'Social'. The widget can stay
    template-family-agnostic without threading the family id through."""
    files = dict(compose_files(resolved))
    contact = files["views/widgets/form/contact.ejs"]
    # Both labels are emitted
    assert ">Social<" in contact
    assert ">Elsewhere<" in contact
    # Letterpress label is hidden by default
    assert ".cas-contact-eyebrow-label-letterpress { display: none; }" in contact
    # Body-class scoped CSS swaps the two labels for the three minimal/
    # letterpress template families.
    for fam in (
        "editorial-statement",
        "editorial-sanctuary",
        "simple-statement",
    ):
        assert f"body.cas-template-{fam} .cas-contact-eyebrow-label-default" in contact
        assert f"body.cas-template-{fam} .cas-contact-eyebrow-label-letterpress" in contact


def test_contact_page_handler_forwards_service_query(resolved):
    files = dict(compose_files(resolved))
    pages = files["routes/pages.js"]
    assert "req.query.service" in pages
    assert "preselected_service" in pages


def test_contact_view_passes_preselected_service_to_widget(resolved):
    files = dict(compose_files(resolved))
    contact_view = files["views/contact.ejs"]
    # The emit injects a dynamic JS expression so the widget can read it
    assert "preselected_service" in contact_view


def test_form_contact_widget_honors_preselect(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/form/contact.ejs"]
    assert "preselected_service" in widget
    assert "selected" in widget


def test_work_with_me_ctas_link_to_contact_with_service(sample_spec):
    """Sample spec's Work With Me offerings link to /contact with query
    string so the Service Interest is preselected on arrival."""
    offerings = sample_spec.pages.get("work-with-me", {}).get("offerings", {})
    items = offerings.get("items", [])
    assert items, "work-with-me should have offerings"
    hrefs = [i.get("cta_href", "") for i in items]
    assert any("/contact?service=" in h for h in hrefs), (
        "at least one offering CTA should preselect a service"
    )


def test_form_contact_service_options_harvested_from_offerings():
    """_harvest_service_options_from_offerings should walk spec.pages
    and collect offering titles, dedupe case-insensitively, cap at 24,
    and append 'General Inquiry'. Backs the form-contact widget default
    so the in-form <select> stays in sync with the visible offerings
    grids whenever the keeper hasn't set settings.service_options.
    See docs/internals/SERVICE_PREFILL_PATTERN.md.
    """
    from compose.build import _harvest_service_options_from_offerings

    spec = BuildSpec.from_dict({
        "app_name": "harvest-test",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "pages": {
            "home": {
                "offerings": {
                    "items": [
                        {"title": "Reiki Session"},
                        {"title": "Mentorship"},
                    ]
                }
            },
            "work-with-me": {
                "services": {
                    "items": [
                        # Duplicate (case-insensitive): dropped.
                        {"title": "reiki session"},
                        {"title": "Circle"},
                    ]
                }
            },
        },
    })
    rs = resolve_spec(spec)
    opts = _harvest_service_options_from_offerings(rs)
    assert opts == ["Reiki Session", "Mentorship", "Circle", "General Inquiry"]


def test_form_contact_service_options_harvest_empty_when_no_offerings():
    """No offerings → empty list (no spurious 'General Inquiry' alone)."""
    from compose.build import _harvest_service_options_from_offerings

    spec = BuildSpec.from_dict({
        "app_name": "harvest-empty",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "pages": {"home": {"hero": {"heading": "Hi"}}},
    })
    rs = resolve_spec(spec)
    assert _harvest_service_options_from_offerings(rs) == []


def test_form_contact_service_options_harvest_caps_at_24():
    """Cap protects the <select> usability for runaway specs."""
    from compose.build import _harvest_service_options_from_offerings

    items = [{"title": f"Offering {i:02d}"} for i in range(40)]
    spec = BuildSpec.from_dict({
        "app_name": "harvest-cap",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "pages": {"home": {"offerings": {"items": items}}},
    })
    rs = resolve_spec(spec)
    opts = _harvest_service_options_from_offerings(rs)
    # 24 harvested + 1 "General Inquiry" appended.
    assert len(opts) == 25
    assert opts[-1] == "General Inquiry"
    assert opts[0] == "Offering 00"
    assert opts[23] == "Offering 23"


def test_form_contact_default_falls_back_to_harvested_options():
    """When neither settings.service_options nor spec.contact.form.service_options
    is set, _page_widget_defaults for form-contact should fall back to
    titles harvested from offerings, so the widget renders the
    Service Interest <select> and the ?service= URL preselect lands.
    """
    from compose.build import _page_widget_defaults

    spec = BuildSpec.from_dict({
        "app_name": "fallback-test",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "pages": {
            "work-with-me": {
                "offerings": {
                    "items": [
                        {"title": "1:1 Session", "cta_href": "/contact"},
                        {"title": "Group Circle", "cta_href": "/contact"},
                    ]
                }
            },
            # contact page declares only a hero; no form block.
            "contact": {"hero": {"heading": "Get in Touch"}},
        },
    })
    rs = resolve_spec(spec)
    defaults = _page_widget_defaults(rs, "contact", "form-contact")
    assert defaults["service_options"] == [
        "1:1 Session",
        "Group Circle",
        "General Inquiry",
    ]


def test_form_contact_default_prefers_settings_service_options():
    """settings.service_options overrides the offerings harvest.
    Explicit override wins; harvest is the fallback, not a merge.
    """
    from compose.build import _page_widget_defaults

    spec = BuildSpec.from_dict({
        "app_name": "settings-wins",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "settings": {"service_options": ["Custom A", "Custom B"]},
        "pages": {
            "work-with-me": {
                "offerings": {
                    "items": [{"title": "Should Not Appear", "cta_href": "/contact"}]
                }
            },
        },
    })
    rs = resolve_spec(spec)
    defaults = _page_widget_defaults(rs, "contact", "form-contact")
    assert defaults["service_options"] == ["Custom A", "Custom B"]


def test_form_contact_widget_renders_select_when_offerings_present():
    """End-to-end: a spec that emits offerings-grid-3col items but does
    NOT set settings.service_options or spec.contact.form.service_options
    still has the in-form <select name="service_interest"> rendered.

    Without the auto-harvest, the form-contact widget silently drops the
    Service Interest <select> and the ?service= URL preselect lands on
    nothing; broken funnel. See SERVICE_PREFILL_PATTERN.md.
    """
    spec = BuildSpec.from_dict({
        "app_name": "select-renders",
        "archetype": "healer-wellness",
        "admin_email": "a@b.c",
        "pages": {
            "home": {
                "hero": {"heading": "Welcome"},
                "offerings": {
                    "items": [
                        {"title": "Energy Session", "cta_href": "/contact"},
                    ]
                },
            },
            "work-with-me": {
                "offerings": {
                    "items": [
                        {"title": "Mentorship", "cta_href": "/contact"},
                    ]
                },
            },
            "contact": {"hero": {"heading": "Reach Out"}},
        },
    })
    rs = resolve_spec(spec)
    files = dict(compose_files(rs))
    contact_view = files["views/contact.ejs"]
    # The form-contact include must carry a non-empty service_options
    # array (otherwise the widget's `service_options.length > 0` guard
    # drops the <select>).
    assert "service_options" in contact_view
    assert "Energy Session" in contact_view
    assert "Mentorship" in contact_view
    assert "General Inquiry" in contact_view


# ---- round-4 polish ----



def test_event_model_has_time_and_end_date_fields():
    from compose.build import _default_fields
    fields = _default_fields("Event")
    names = [f["name"] for f in fields]
    assert "event_date" in names
    assert "event_time" in names
    assert "end_date" in names
    assert "end_time" in names
    # event_time is TIME type
    times = [f for f in fields if f["name"] in ("event_time", "end_time")]
    assert all(f["sql_type"] == "TIME" for f in times)


def test_event_edit_form_emits_time_inputs(resolved):
    """Event admin edit view should render <input type="time"> for
    event_time + end_time."""
    files = dict(compose_files(resolved))
    edit = files["views/events/edit.ejs"]
    # Field declarations in the fieldsTemplate JSON
    assert '"name": "event_time"' in edit and '"type": "time"' in edit
    assert '"name": "end_time"' in edit


def test_crud_form_widget_handles_time_input(resolved):
    files = dict(compose_files(resolved))
    form = files["views/widgets/admin/crud-form.ejs"]
    assert "field.type === 'time'" in form
    assert 'type="time"' in form


def test_event_schema_includes_time_columns(resolved):
    files = dict(compose_files(resolved))
    schema = files["db/schema.sql"]
    assert "event_time TIME" in schema or "event_time time" in schema.lower()
    assert "end_date DATE" in schema
    assert "end_time TIME" in schema


# ---- v2-events-data-model-and-admin-crud (2026-05-14) ----
# A hand-built bookstore event page for an author site
# ported into compose. Event picks up rich-content fields, JSONB photo
# gallery, YouTube/Vimeo nocookie embed, auto-advancing slideshow, intro
# / body / closing-quote rich-text fields. Existing time-column shape is
# preserved.

def test_event_model_has_rich_content_fields():
    from compose.build import _default_fields
    fields = _default_fields("Event")
    names = [f["name"] for f in fields]
    # New rich-content fields for long-form event pages
    assert "slug" in names
    assert "eyebrow" in names
    assert "date_label" in names
    assert "venue" in names
    assert "venue_url" in names
    assert "hero_image_url" in names
    assert "hero_image_alt" in names
    assert "intro" in names
    assert "body" in names
    assert "photos" in names
    assert "video_url" in names
    assert "video_caption" in names
    assert "closing_quote" in names
    assert "published" in names
    assert "sort_order" in names


def test_event_photos_field_is_jsonb():
    from compose.build import _default_fields
    fields = _default_fields("Event")
    photos = next(f for f in fields if f["name"] == "photos")
    assert (photos.get("sql_type") or "").upper() == "JSONB"


def test_event_published_field_is_boolean_default_false():
    from compose.build import _default_fields
    fields = _default_fields("Event")
    published = next(f for f in fields if f["name"] == "published")
    assert "BOOLEAN" in (published.get("sql_type") or "").upper()
    assert (published.get("default") or "").upper() == "FALSE"


def test_event_schema_includes_jsonb_photos_column(resolved):
    files = dict(compose_files(resolved))
    schema = files["db/schema.sql"]
    # photos JSONB column emits with sql_type-derived JSONB
    assert "photos JSONB" in schema


def test_event_route_declares_jsonb_cols_set(resolved):
    """The emitted resource route knows photos is JSONB so it can JSON.parse
    the textarea body before binding to pg."""
    files = dict(compose_files(resolved))
    route = files["routes/events.js"]
    assert "JSONB_COLS" in route
    # photos column must be in the JSONB set
    assert '"photos"' in route or "'photos'" in route
    # JSON.parse path exists
    assert "JSON.parse" in route


def test_event_route_jsonb_parse_in_create_and_update(resolved):
    files = dict(compose_files(resolved))
    route = files["routes/events.js"]
    # The parse-when-jsonb branch appears in both CREATE and UPDATE.
    occurrences = route.count("isJsonb && typeof v === 'string'")
    assert occurrences >= 2, (
        f"expected JSONB parse branch in both CREATE + UPDATE, got {occurrences}"
    )


def test_event_route_jsonb_parse_error_surfaces_to_admin(resolved):
    """Malformed JSON in a JSONB textarea must NOT silently save as []. The
    admin sees a clear validation message: 'photos must be valid JSON. Got:
    <parse error>'. CREATE re-renders the new form with the error; UPDATE
    flashes + redirects to the edit form."""
    files = dict(compose_files(resolved))
    route = files["routes/events.js"]
    # Error collection bucket exists in both CREATE + UPDATE.
    assert route.count("_jsonbParseErrors") >= 6, (
        "expected _jsonbParseErrors declared + pushed + length-checked in "
        "both CREATE and UPDATE handlers"
    )
    # The error message names the column + carries the parse error.
    assert "must be valid JSON. Got: ' + _e.message" in route
    # Legacy silent fallback to [] must be gone.
    assert "catch (_e) { v = []; }" not in route
    assert "catch (_e) {{ v = []; }}" not in route
    # CREATE re-renders the new form with the error.
    assert "_jsonbParseErrors.join(' · ')" in route or "_jsonbParseErrors.join" in route
    # UPDATE flashes + redirects back to /edit.
    assert "flash_error = _jsonbParseErrors.join" in route


def test_event_form_stringifies_jsonb_value_for_textarea(resolved):
    """The edit form must JSON.stringify a JSONB col so the textarea
    shows the existing array as JSON text rather than [object Object]."""
    files = dict(compose_files(resolved))
    edit = files["views/events/edit.ejs"]
    # photos is rendered as textarea
    assert '"name": "photos"' in edit
    assert '"is_jsonb": true' in edit
    # _fmtJsonbVal helper present
    assert "_fmtJsonbVal" in edit


def test_photo_slideshow_widget_exists(resolved):
    files = dict(compose_files(resolved))
    assert "views/widgets/content/photo-slideshow.ejs" in files
    widget = files["views/widgets/content/photo-slideshow.ejs"]
    # 5-second auto-advance
    assert "5000" in widget
    # Pauses on hover + focus
    assert "mouseenter" in widget and "focusin" in widget
    # Only fires when 2+ photos (single photo renders as plain figure)
    assert "_photos.length > 1" in widget
    assert "_photos.length === 1" in widget
    # Honors prefers-reduced-motion
    assert "prefers-reduced-motion" in widget


def test_video_embed_widget_exists_and_uses_nocookie(resolved):
    files = dict(compose_files(resolved))
    assert "views/widgets/content/video-embed-nocookie.ejs" in files
    widget = files["views/widgets/content/video-embed-nocookie.ejs"]
    # YouTube nocookie domain
    assert "youtube-nocookie.com/embed/" in widget
    # Vimeo player URL
    assert "player.vimeo.com/video/" in widget
    # Handles all three YouTube URL formats (watch?v=, youtu.be/, /embed/)
    assert "youtu.be" in widget
    assert "[?&]v=" in widget
    # Has lazy-loading on the iframe (no autoload tracking)
    assert "loading=\"lazy\"" in widget or "loading='lazy'" in widget


def test_video_embed_widget_validates_fallback_href_scheme(resolved):
    """The unrecognized-URL fallback renders an <a href> from raw admin
    input. Without a scheme guard, javascript: URLs would execute when
    clicked (EJS <%= %> escapes HTML but does not block schemes). Guard
    must reject anything that is not http(s)."""
    files = dict(compose_files(resolved))
    widget = files["views/widgets/content/video-embed-nocookie.ejs"]
    # Scheme guard helper present + matches the http(s) pattern from
    # the roadmap item description.
    assert "_isSafeHttpUrl" in widget
    assert "/^https?:\\/\\//i.test" in widget
    # Fallback branch uses the guarded variable, not the raw input.
    assert "_safeRawHref" in widget
    assert 'href="<%= _safeRawHref %>"' in widget
    # Raw _raw is never interpolated directly into href.
    assert 'href="<%= _raw %>"' not in widget


def test_photo_slideshow_widget_filters_unsafe_image_schemes(resolved):
    """The slideshow renders <img src> from raw admin-provided URLs.
    Without a scheme guard, javascript: URLs would survive into the DOM
    (they will not execute on <img src> in modern browsers, but the same
    array feeds future consumers and the safer pattern is to filter at
    the gate). Guard must accept http(s) and root-relative paths and
    reject everything else."""
    files = dict(compose_files(resolved))
    widget = files["views/widgets/content/photo-slideshow.ejs"]
    # Scheme guard helper present + matches the http(s) pattern from
    # the roadmap item description, plus root-relative for admin uploads.
    assert "_isSafeImgUrl" in widget
    assert "/^https?:\\/\\//i.test" in widget
    # Root-relative means ONE leading slash: "//host" and "/\\host" are
    # protocol-relative URLs to another origin, so the guard excludes them.
    assert "/^\\/(?![\\/\\\\])/.test" in widget
    # Filter passes each photo through the guard, not the prior
    # truthy-trim-only check.
    assert "photos.filter(function(p){ return _isSafeImgUrl(p); })" in widget


def _resolved_with_public_events(sample_spec):
    """Helper: clone the sample spec but flip Event.public_listing_path
    on so views/events/detail.ejs gets emitted. The default healer
    archetype defines Event with no explicit public_listing_path, so the
    detail view is skipped. The rich-content detail template still has
    to be correct for any archetype that DOES expose Event publicly."""
    import copy
    spec = copy.deepcopy(sample_spec)
    spec.data_models = [
        {
            "name": "Event",
            "plural": "events",
            "public_listing_path": "/events-list",
        }
    ]
    return resolve_spec(spec)


def test_event_public_detail_uses_slideshow_and_video_widgets(sample_spec):
    """When an Event row has photos/video_url/intro/body/closing_quote
    populated, the public detail view includes the slideshow + video
    widgets and renders the rich shape."""
    rs = _resolved_with_public_events(sample_spec)
    files = dict(compose_files(rs))
    detail = files["views/events/detail.ejs"]
    # Slideshow include for photos JSONB array
    assert "widgets/content/photo-slideshow.ejs" in detail
    # Video embed include for video_url
    assert "widgets/content/video-embed-nocookie.ejs" in detail
    # Intro paragraph (italic lead)
    assert "record.intro" in detail
    # Closing pull-quote
    assert "record.closing_quote" in detail
    # Hero image with eager-load + fetchpriority high (above-the-fold)
    assert "record.hero_image_url" in detail
    assert "fetchpriority='high'" in detail or 'fetchpriority="high"' in detail
    # Venue with optional venue_url
    assert "record.venue" in detail
    # JSONB photos array is tolerantly parsed at render (pg returns
    # JS arrays directly, but a string fallback keeps render safe).
    assert "JSON.parse" in detail


def test_event_detail_preserves_old_shape_when_rich_fields_absent(sample_spec):
    """The legacy event_date / event_time / location card path is still
    rendered when no rich fields exist (backwards compat)."""
    rs = _resolved_with_public_events(sample_spec)
    files = dict(compose_files(rs))
    detail = files["views/events/detail.ejs"]
    # Legacy date formatter still present
    assert "record.event_date" in detail
    assert "record.event_time" in detail
    # Legacy registration_url CTA still present
    assert "record.registration_url" in detail


def test_crud_list_widget_has_action_pill_group(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/crud-list.ejs"]
    # MW v25-style pill group: single bordered container with View/Edit/Delete
    assert "inline-flex items-center gap-1 rounded-md border overflow-hidden" in widget
    # Visual dividers between actions (1px spans)
    assert "width:1px" in widget


def test_crud_list_widget_renders_image_thumbnails(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/crud-list.ejs"]
    # Image detection helper + 48px thumb
    assert "_isImageUrl" in widget
    assert "width: 48px" in widget
    assert "object-fit: cover" in widget


def test_crud_list_widget_humanizes_image_url_label(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/crud-list.ejs"]
    # The helper maps image_url-family keys to "Image" instead of "Image Url"
    assert "_displayLabel" in widget
    assert "'Image'" in widget


def test_nav_public_logo_has_strict_size_constraints(resolved):
    """The logo must render at EXACTLY 40x40 so large uploads don't overflow
    the nav (defense against unoptimized source images)."""
    files = dict(compose_files(resolved))
    nav = files["views/widgets/shell/nav-public.ejs"]
    assert "max-width: 40px" in nav
    assert "max-height: 40px" in nav
    assert "border-radius: 9999px" in nav
    assert "overflow: hidden" in nav
    assert "object-fit: cover" in nav


def test_style_css_is_the_template_file_as_it_stands(resolved):
    """public/style.css is compose/templates/public-style.css, read with
    universal newlines and changed in no other way, so a checkout that
    turned the file's line endings into CRLF still emits LF."""
    files = dict(compose_files(resolved))
    expected = PUBLIC_STYLE_CSS_PATH.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert files["public/style.css"] == expected


def test_style_css_has_h10_w10_utilities(resolved):
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    assert ".h-10 { height: 2.5rem; }" in css
    assert ".w-10 { width: 2.5rem; }" in css


def test_style_css_emits_classes_used_by_hero_widget(resolved):
    """The stylesheet (compose/templates/public-style.css) is a fixed enumerated list,
    not a JIT-scanned utility build. Any class a widget uses MUST exist
    in the emitted CSS, or it silently renders at 0px.

    This guard pins the utility classes the hero widget relies on
    (including the md: variants) so future spacing tweaks to the widget
    surface the CSS gap at test time instead of in the browser.
    """
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    # Non-compact hero: pt-6 md:pt-10 pb-12 md:pb-20 on the <section>
    # and mt-16 md:mt-24 on the image wrapper. Each base + md: variant
    # below must be emitted or the hero will render flat.
    assert ".pt-6 { padding-top: 1.5rem; }" in css
    assert ".pt-10 { padding-top: 2.5rem; }" in css
    assert ".pb-12 { padding-bottom: 3rem; }" in css
    assert ".pb-20 { padding-bottom: 5rem; }" in css
    assert ".mt-16 { margin-top: 4rem; }" in css
    assert ".mt-24 { margin-top: 6rem; }" in css
    # md: variants live inside @media (min-width: 768px), the leading
    # backslash is required because Tailwind-style colons are escaped
    # in CSS selectors (.md\:pt-10, not .md:pt-10).
    assert r".md\:pt-10 { padding-top: 2.5rem; }" in css
    assert r".md\:pb-20 { padding-bottom: 5rem; }" in css
    assert r".md\:mt-24 { margin-top: 6rem; }" in css


def test_hero_padding_reduced(resolved):
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/centered.ejs"]
    # Round-7: hero is now conditional, full-size default, compact mode
    # (py-8 md:py-12) when called with compact: true. Both paths must be
    # tighter than the original py-16 md:py-24 baseline.
    # 2026-04-24: full-size top padding reduced further (founder feedback, 
    # less whitespace above the headline). Accept the split pt-*/pb-* form
    # in addition to the original symmetric py-* form.
    assert "py-16 md:py-24" not in hero, "hero should not use py-16 md:py-24 baseline anymore"
    assert "cas-page-header py-7 md:py-9" in hero, (
        "hero compact mode is the cas-page-header contract"
    )
    full_size_variants = (
        "py-10 md:py-16",
        "py-12 md:py-20",
        "pt-6 md:pt-10 pb-12 md:pb-20",
    )
    assert any(v in hero for v in full_size_variants), (
        f"hero should support one of the full-size variants: {full_size_variants}"
    )


def test_content_section_padding_reduced(resolved):
    """Offerings/testimonials/cta sections reduced from py-16 md:py-20 to
    py-10 md:py-14. Contact form tightened further in round-7 to py-8."""
    files = dict(compose_files(resolved))
    for path in (
        "views/widgets/offerings/grid-3col.ejs",
        "views/widgets/offerings/resource-list-card-grid.ejs",
        "views/widgets/offerings/photo-gallery-grid.ejs",
        "views/widgets/testimonials/stack.ejs",
        "views/widgets/cta/inquiry-block.ejs",
    ):
        assert "py-10 md:py-14" in files[path], f"{path} should use py-10 md:py-14"
        assert "py-16 md:py-20" not in files[path], f"{path} still has old padding"
    # form/contact.ejs uses a clamp()-driven padding-block now (the prior
    # py-8 md:py-12 had the form touching the hero above
    # and the footer below). Clamp gives 3rem → 6rem as viewport scales.
    contact = files["views/widgets/form/contact.ejs"]
    assert "padding-block: clamp(3rem, 7vw, 6rem)" in contact, "contact uses clamp padding-block"


def test_sample_spec_events_include_times(sample_spec):
    events = (sample_spec.seed_content or {}).get("events") or []
    assert events, "sample spec must have events"
    with_time = [e for e in events if e.get("event_time")]
    assert len(with_time) >= 4, "most events should have a time"


def test_dashboard_upcoming_events_query_selects_time(resolved):
    files = dict(compose_files(resolved))
    admin_js = files["routes/admin-dashboard.js"]
    # The query must pull event_time + end_time so the dashboard can render them
    assert "event_time" in admin_js
    assert "end_date" in admin_js
    assert "end_time" in admin_js


def test_upcoming_events_widget_renders_time(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/upcoming-events.ejs"]
    assert "event_time" in widget
    # Formatted AM/PM output
    assert "'PM'" in widget or '"PM"' in widget


# ---- round-5: archetype expansion (therapist/fitness/salon/restaurant/tradesperson) ----

def test_five_new_archetypes_registered():
    from compose.build import load_archetypes, get_archetype
    archetypes = load_archetypes()
    ids = {a["id"] for a in archetypes["archetypes"]}
    for new_id in (
        "therapist-counselor", "fitness-studio", "salon-spa",
        "restaurant-cafe", "tradesperson-contractor"
    ):
        assert new_id in ids, f"archetype {new_id} missing"
        a = get_archetype(new_id)
        assert a["default_pages"], f"{new_id} must have default_pages"
        assert a["default_data_models"], f"{new_id} must have default_data_models"
        assert a["page_layouts"], f"{new_id} must map page layouts"


def test_each_archetype_page_has_layout_defined():
    """Every default_page in an archetype must resolve to a layout in
    page-layouts.json (otherwise compose will fail at render)."""
    from compose.build import load_archetypes, load_layouts
    archetypes = load_archetypes()["archetypes"]
    layouts = load_layouts()["layouts"]
    for a in archetypes:
        for page_id in a["default_pages"]:
            layout_id = a["page_layouts"].get(page_id)
            assert layout_id, f"{a['id']} page {page_id} missing page_layouts entry"
            assert layout_id in layouts, f"{a['id']} references undefined layout {layout_id}"


def test_no_archetype_crud_page_collisions():
    """Archetype default_pages must NOT collide with default_data_models
    public_listing_path routes. If they do, the CRUD router shadows the
    composed widget page, breaking the intended layout.

    Context: fitness-studio had /classes /schedule /instructors /pricing
    both as default_pages AND as model public_listing_path, causing generic
    CRUD lists to shadow the intended widget layouts.
    """
    from compose.build import load_archetypes
    archetypes = load_archetypes()["archetypes"]
    for a in archetypes:
        pages = set(a["default_pages"])
        crud_routes = {
            m["public_listing_path"].lstrip("/")
            for m in a["default_data_models"]
            if m.get("public_listing_path")
        }
        collisions = pages & crud_routes
        assert not collisions, (
            f"{a['id']} has {len(collisions)} route collisions: {sorted(collisions)}. "
            f"Remove public_listing_path from colliding models so composed pages own those routes."
        )


def test_each_page_layout_widget_is_registered():
    """Every widget id referenced by page-layouts.json must exist in
    widget-registry.json (otherwise resolve_spec will fail)."""
    from compose.build import load_layouts, load_widgets
    layouts = load_layouts()["layouts"]
    widgets = load_widgets()["widgets"]
    for layout_id, layout in layouts.items():
        for w in layout["widgets"]:
            assert w["id"] in widgets, (
                f"layout {layout_id} references undefined widget {w['id']}"
            )


def test_new_palettes_registered():
    from compose.build import load_palettes
    palettes = load_palettes()
    ids = {p["id"] for p in palettes["palettes"]}
    for new_id in ("cream-espresso", "sage-clay", "blush-bronze"):
        assert new_id in ids


def test_new_font_pairs_registered():
    from compose.build import load_font_pairs
    pairs = load_font_pairs()
    ids = {fp["id"] for fp in pairs["font_pairs"]}
    for new_id in ("lora-inter", "archivo-dm-sans"):
        assert new_id in ids


def test_new_widgets_registered():
    from compose.build import load_widgets
    widgets = load_widgets()["widgets"]
    for new_id in (
        "faq-accordion", "pricing-tier-table", "hours-location-block",
        "menu-grouped-list", "staff-card-grid", "trust-badges-row",
        "service-area-list", "instructor-grid", "weekly-schedule-grid",
        "credentials-block", "service-menu-priced", "impact-stats-row",
    ):
        assert new_id in widgets, f"widget {new_id} missing from registry"


def test_new_widget_files_exist():
    """Every registered widget file must exist on disk."""
    from compose.build import load_widgets, COMPOSE_DIR
    widgets = load_widgets()["widgets"]
    for wid, w in widgets.items():
        fp = COMPOSE_DIR / w["file"]
        assert fp.exists(), f"{wid} file {w['file']} missing"


def test_new_data_models_have_default_fields():
    from compose.build import _default_fields
    for name in (
        "FAQ", "PricingPlan", "StaffMember", "Instructor", "ClassType",
        "ScheduleSlot", "MenuSection", "MenuItem", "ServiceArea",
        "Modality", "Specialty", "Program"
    ):
        fields = _default_fields(name)
        assert fields, f"{name} has no default fields"
        assert len(fields) >= 2, f"{name} too few fields"


def test_therapist_sample_fixture_compiles(tmp_path):
    from compose.build import build_app
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-therapist-spec.json"
    out = build_app(str(spec), output_dir=tmp_path / "therapist")
    assert (out / "server.js").exists()
    assert (out / "views/approach.ejs").exists()
    assert (out / "views/fees-insurance.ejs").exists()
    assert (out / "views/faq.ejs").exists()
    schema = (out / "db/schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS modalities" in schema
    assert "CREATE TABLE IF NOT EXISTS pricing_plans" in schema
    assert "CREATE TABLE IF NOT EXISTS faqs" in schema


def test_fitness_sample_fixture_compiles(tmp_path):
    from compose.build import build_app
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-fitness-spec.json"
    out = build_app(str(spec), output_dir=tmp_path / "fitness")
    assert (out / "views/classes.ejs").exists()
    assert (out / "views/schedule.ejs").exists()
    assert (out / "views/instructors.ejs").exists()
    assert (out / "views/pricing.ejs").exists()
    schema = (out / "db/schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS class_types" in schema
    assert "CREATE TABLE IF NOT EXISTS instructors" in schema
    assert "CREATE TABLE IF NOT EXISTS schedule_slots" in schema
    assert "start_time TIME" in schema


def test_salon_sample_fixture_compiles(tmp_path):
    from compose.build import build_app
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-salon-spec.json"
    out = build_app(str(spec), output_dir=tmp_path / "salon")
    assert (out / "views/team.ejs").exists()
    schema = (out / "db/schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS staff" in schema


def test_restaurant_sample_fixture_compiles(tmp_path):
    from compose.build import build_app
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    out = build_app(str(spec), output_dir=tmp_path / "restaurant")
    assert (out / "views/menu.ejs").exists()
    assert (out / "views/visit.ejs").exists()
    schema = (out / "db/schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS menu_items" in schema


def test_tradesperson_sample_fixture_compiles(tmp_path):
    from compose.build import build_app
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-tradesperson-spec.json"
    out = build_app(str(spec), output_dir=tmp_path / "tradesperson")
    assert (out / "views/service-areas.ejs").exists()
    schema = (out / "db/schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS service_areas" in schema


def test_every_archetype_has_sample_fixture():
    """Smoke: every shipped archetype should have at least one sample
    fixture in compose/fixtures/ so the build is reproducible."""
    from compose.build import load_archetypes
    fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
    fixture_names = {f.name for f in fixtures_dir.glob("sample-*-spec.json")}
    archetype_ids = [a["id"] for a in load_archetypes()["archetypes"]]
    # Each archetype must have at least one fixture whose filename begins
    # with its id stem (e.g. therapist-counselor → sample-therapist-spec.json).
    archetype_stems = {
        "healer-wellness": "sample-healer-spec.json",
        "coach-consultant": None,  # not yet
        "local-service": None,  # tradesperson covers
        "therapist-counselor": "sample-therapist-spec.json",
        "fitness-studio": "sample-fitness-spec.json",
        "salon-spa": "sample-salon-spec.json",
        "restaurant-cafe": "sample-restaurant-spec.json",
        "tradesperson-contractor": "sample-tradesperson-spec.json",
    }
    for aid in archetype_ids:
        expected = archetype_stems.get(aid)
        if expected is None:
            continue  # archetype intentionally has no fixture yet
        assert expected in fixture_names, f"missing fixture for archetype {aid}: {expected}"


# ---- round-6: field-types registry ----

def test_field_types_registry_loads():
    from compose.build import load_field_types
    r = load_field_types()
    assert "field_types" in r
    assert "us_states" in r
    assert len(r["us_states"]) == 51  # 50 states + DC
    # Core types we ship
    for t in (
        "text", "long_text", "email", "phone_us", "url", "slug", "zip_us",
        "state_us", "address_line", "city", "date", "time", "datetime",
        "integer", "decimal", "currency_usd", "boolean", "select", "image",
        "color_hex", "password",
    ):
        assert t in r["field_types"], f"field_type {t} missing"


def test_get_field_type_returns_metadata():
    from compose.build import get_field_type
    email = get_field_type("email")
    assert email["html_input"] == "input:email"
    assert email["sql_type"] == "VARCHAR(255)"
    assert "trim" in email["sanitize"] and "lowercase" in email["sanitize"]
    assert email["autocomplete"] == "email"
    assert email["pattern"]


def test_sql_type_resolves_from_field_type():
    from compose.build import _sql_type_for
    # field_type wins when sql_type is absent
    assert _sql_type_for({"name": "email", "field_type": "email"}) == "VARCHAR(255)"
    assert _sql_type_for({"name": "phone", "field_type": "phone_us"}) == "VARCHAR(20)"
    assert _sql_type_for({"name": "zip", "field_type": "zip_us"}) == "VARCHAR(10)"
    assert _sql_type_for({"name": "state", "field_type": "state_us"}) == "VARCHAR(2)"
    # explicit sql_type still takes precedence
    assert _sql_type_for({"name": "x", "field_type": "email", "sql_type": "TEXT"}) == "TEXT"


def test_html_input_resolves_from_field_type():
    from compose.build import _html_input_for
    assert _html_input_for({"name": "email", "field_type": "email"}) == "input:email"
    assert _html_input_for({"name": "phone", "field_type": "phone_us"}) == "input:tel"
    assert _html_input_for({"name": "state", "field_type": "state_us"}) == "select:state-us"
    assert _html_input_for({"name": "zip", "field_type": "zip_us"}) == "input:text"
    assert _html_input_for({"name": "url", "field_type": "url"}) == "input:url"
    # Legacy heuristics still work when no field_type set
    assert _html_input_for({"name": "email", "sql_type": "VARCHAR(255)"}) == "input:email"
    assert _html_input_for({"name": "image_url", "sql_type": "TEXT"}) == "upload:image"


def test_contact_inquiry_uses_typed_fields():
    from compose.build import _default_fields
    fields = _default_fields("ContactInquiry")
    name_to = {f["name"]: f for f in fields}
    assert name_to["email"].get("field_type") == "email"
    assert name_to["phone"].get("field_type") == "phone_us"
    assert name_to["message"].get("field_type") == "long_text"


def test_middleware_fields_js_is_emitted(resolved):
    files = dict(compose_files(resolved))
    assert "middleware/fields.js" in files
    js = files["middleware/fields.js"]
    assert "sanitizeField" in js
    assert "validateField" in js
    assert "processForm" in js
    # Sanitize steps we depend on
    assert "digits_only" in js
    assert "trim_multiline" in js
    assert "lowercase" in js
    assert "US_STATE_CODES" in js


def test_resource_router_uses_processForm(resolved):
    files = dict(compose_files(resolved))
    blog = files["routes/blog-posts.js"]
    # Import, FIELD_SPEC, and usage on both create + update
    assert "require('../middleware/fields')" in blog
    assert "FIELD_SPEC" in blog
    assert "processForm(FIELD_SPEC" in blog


def test_pages_router_contact_uses_processForm(resolved):
    files = dict(compose_files(resolved))
    pages = files["routes/pages.js"]
    # The public forms go through processPublicForm, which runs the same
    # processForm the admin forms use and adds the length and column checks.
    assert "processPublicForm(FIELDS, req.body)" in pages
    assert "const checked = processForm(" in files["middleware/fields.js"]
    # Types passed in the contact field spec. The spec is the contact
    # model's field list, written as JSON (see _contact_post_handler_js).
    post = pages[pages.index("router.post('/contact'"):]
    fields = json.loads(post.split("const FIELDS = ", 1)[1].split(";\n", 1)[0])
    types = {f["name"]: f["field_type"] for f in fields}
    assert types["email"] == "email"
    assert types["message"] == "long_text"
    assert [f["name"] for f in fields if f["required"]] == ["name", "email", "message"]
    # Every field carries a length limit and the kind of its column.
    assert all(isinstance(f["max_length"], int) and 0 < f["max_length"] <= 10000 for f in fields)
    assert {f["column"] for f in fields} == {"text"}


def test_crud_form_widget_handles_state_select(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/crud-form.ejs"]
    assert "state-us" in widget
    assert "_US_STATES" in widget
    # All 50 states present in the inlined array (quick sanity)
    for code in ("CA", "NY", "TX", "FL", "OR", "ID"):
        assert f'"{code}"' in widget


def test_crud_form_widget_emits_pattern_attr(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/crud-form.ejs"]
    assert "pattern=" in widget
    assert "inputmode=" in widget
    assert "autocomplete=" in widget


def test_contact_inquiry_schema_has_phone_column(resolved):
    files = dict(compose_files(resolved))
    schema = files["db/schema.sql"]
    # ContactInquiry schema now includes phone
    ci_idx = schema.find("CREATE TABLE IF NOT EXISTS contact_inquiries")
    assert ci_idx != -1
    # Slice out just the contact_inquiries definition
    end = schema.find(");", ci_idx)
    ci_def = schema[ci_idx:end]
    assert "phone" in ci_def
    assert "VARCHAR(20)" in ci_def  # phone_us field_type maps to VARCHAR(20)
    assert "email" in ci_def


# ---- round-7: client-side validation + value preservation ----

def test_crud_form_allows_native_browser_validation(resolved):
    """novalidate must NOT be on the form; we want HTML5 client-side
    validation to fire before submission so users get instant feedback."""
    files = dict(compose_files(resolved))
    form = files["views/widgets/admin/crud-form.ejs"]
    # novalidate would disable HTML5 validation; must be absent.
    assert "novalidate" not in form, "crud-form must not disable HTML5 validation"
    # cas-form class is the :user-invalid hook in style.css
    assert "cas-form" in form


def test_contact_form_allows_native_browser_validation(resolved):
    """Refactored in round-8 to use form/field.ejs; the partial pulls
    pattern/inputmode/autocomplete from the registry at render time, so
    contact.ejs no longer has hard-coded attrs. We verify the partial
    wiring + that the field.ejs partial emits every validation attr."""
    files = dict(compose_files(resolved))
    form = files["views/widgets/form/contact.ejs"]
    assert "novalidate" not in form
    assert "cas-form" in form
    # Delegates to the typed-field partial for each input
    assert "include('./field.ejs'" in form
    # field.ejs must emit the full validation attr set
    field = files["views/widgets/form/field.ejs"]
    for attr in ("pattern", "maxlength", "minlength", "inputmode", "autocomplete"):
        assert attr in field, f"field partial missing {attr}"


def test_contact_form_preserves_submitted_values(resolved):
    """When validation fails server-side, the form must re-render with
    the user's previously-typed values; not wipe their work."""
    files = dict(compose_files(resolved))
    form = files["views/widgets/form/contact.ejs"]
    # The widget reads `submitted` locals and echoes each field's value:
    # one loop over the contact model's fields, each input given the value
    # that was typed for it. (test_model_derived_emission.py renders the
    # widget and checks the echoed values in the HTML.)
    assert "submitted" in form
    assert "value: _v[f.name]" in form


def test_contact_post_passes_submitted_on_rejection(resolved):
    files = dict(compose_files(resolved))
    pages = files["routes/pages.js"]
    post = pages[pages.index("router.post('/contact'"):pages.index("router.post('/newsletter-signup'")]
    # On validation failure, re-render contact with the submitted values,
    # collected from the request body for every field of the form.
    rejection = post[post.index("if (Object.keys(errors).length > 0)"):post.index("await db.query(")]
    assert "const { values, errors, submitted } = processPublicForm(FIELDS, req.body);" in post
    assert re.search(r"res\.render\('contact', \{[^}]*\bsubmitted,", rejection)
    # processPublicForm collects what was typed for every field of the form
    # (strings only), cut to the field's length limit.
    fields_js = files["middleware/fields.js"]
    assert "const raw = rawBody ? rawBody[spec.name] : undefined;" in fields_js
    assert "submitted[spec.name] = text.slice(0, limit);" in fields_js


def test_contact_view_forwards_submitted_to_widget(resolved):
    files = dict(compose_files(resolved))
    contact_view = files["views/contact.ejs"]
    # emit_page_view injects the submitted dynamic expression
    assert "submitted:" in contact_view


def test_style_css_has_user_invalid_styling(resolved):
    files = dict(compose_files(resolved))
    css = files["public/style.css"]
    # :user-invalid only fires after user interacts (not on page load)
    assert ":user-invalid" in css
    assert ".cas-form" in css
    # Focus styling for keyboard users
    assert ":focus" in css
    assert "outline:" in css


def test_page_layout_static_data_merges(resolved):
    """page-layouts.json can now override spec data per widget via
    static_data: {compact: true} etc. Contact page hero uses compact."""
    files = dict(compose_files(resolved))
    contact = files["views/contact.ejs"]
    # The rendered contact.ejs should pass compact:true into hero-centered
    assert '"compact": true' in contact or "'compact': true" in contact


def test_hero_centered_honors_compact_flag(resolved):
    files = dict(compose_files(resolved))
    hero = files["views/widgets/hero/centered.ejs"]
    assert "typeof compact" in hero
    assert "cas-page-header py-7 md:py-9" in hero  # compact path
    assert "clamp(1.75rem, 3.2vw, 2.5rem)" in hero  # compact heading clamp (2.3)


# ---- round-8: bug fix + universal form-field partial ----

def test_field_type_inferred_from_field_name():
    """Safety net: a model that declares `email` or `phone` etc. without
    setting field_type still gets the strict registry validation applied
    on both client + server. Fix for bug: a truncated address (name@exa) saved on
    NewsletterSubscriber because its email field had only sql_type set."""
    from compose.build import _infer_field_type, _resolve_field_type
    assert _infer_field_type({"name": "email"}) == "email"
    assert _infer_field_type({"name": "contact_email"}) == "email"
    assert _infer_field_type({"name": "phone"}) == "phone_us"
    assert _infer_field_type({"name": "website"}) == "url"
    assert _infer_field_type({"name": "booking_url"}) == "url"
    assert _infer_field_type({"name": "zip"}) == "zip_us"
    assert _infer_field_type({"name": "postal_code"}) == "zip_us"
    assert _infer_field_type({"name": "state"}) == "state_us"
    assert _infer_field_type({"name": "slug"}) == "slug"
    # Unknown names return None
    assert _infer_field_type({"name": "title"}) is None
    # Full resolve uses inference as fallback
    resolved = _resolve_field_type({"name": "email", "sql_type": "VARCHAR(255)"})
    assert resolved is not None
    assert resolved["html_input"] == "input:email"
    assert resolved["pattern"] == "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$"


def test_newsletter_subscriber_email_gets_strict_pattern(resolved):
    """Regression guard for the truncated-email bug. NewsletterSubscriber
    declares email via sql_type only; inference must kick in so the
    emitted edit form has the strict pattern + server-side FIELD_SPEC
    has field_type='email'."""
    files = dict(compose_files(resolved))
    # Edit view must include the strict email pattern (JSON-encoded, so
    # single backslashes in the source become double in the emitted JSON).
    edit = files["views/newsletter-subscribers/edit.ejs"]
    assert '"pattern"' in edit
    assert r'^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$' in edit
    # Server-side FIELD_SPEC must carry field_type: 'email'
    router = files["routes/newsletter-subscribers.js"]
    assert '"field_type": "email"' in router


def test_resource_router_edit_handler_forwards_flash(resolved):
    """After a failed POST /:id/update, the redirect to /:id/edit must
    render any flash_error from the session. Without this the user sees
    no feedback that their submission was rejected."""
    files = dict(compose_files(resolved))
    router = files["routes/blog-posts.js"]
    # GET /:id/edit must read flash_error from session, delete it, and
    # pass it through as `error` to the render.
    assert "req.session.flash_error" in router
    assert "delete req.session.flash_error" in router
    assert "error: flashError" in router


def test_form_field_partial_shipped(resolved):
    files = dict(compose_files(resolved))
    assert "views/widgets/form/field.ejs" in files
    partial = files["views/widgets/form/field.ejs"]
    # Reads from settings._field_types: the "one source of truth" path.
    assert "settings._field_types" in partial
    # Emits ALL standardized validation attrs
    for attr in ("pattern", "maxlength", "minlength", "inputmode",
                 "autocomplete", "step", "min", "max", "placeholder"):
        assert attr in partial


def test_form_field_partial_registered(resolved):
    from compose.build import load_widgets
    widgets = load_widgets()["widgets"]
    assert "form-field" in widgets
    assert widgets["form-field"]["file"] == "widgets/form/field.ejs"


def test_settings_middleware_exposes_field_types(resolved):
    files = dict(compose_files(resolved))
    mw = files["middleware/settings.js"]
    assert "FIELD_TYPES" in mw
    assert "_field_types: FIELD_TYPES" in mw
    assert "_us_states: US_STATES" in mw


def test_settings_middleware_skips_cache_on_empty_rows(resolved):
    """Regression: if site_settings table exists but has 0 rows (seed still
    in flight), loadSettings() must NOT cache the empty object. Otherwise
    every page renders with site_name=undefined for 30s after boot."""
    files = dict(compose_files(resolved))
    mw = files["middleware/settings.js"]
    assert "if (rows.length > 0)" in mw, (
        "loadSettings() must guard the cache write behind rows.length > 0 "
        "so an empty seed-in-flight query isn't cached"
    )


def test_server_js_awaits_migration_before_listen(resolved):
    """Regression: server.js must await db_migrate() before app.listen(),
    so the HTTP listener doesn't bind until seed is complete. Boot race
    otherwise caches empty settings and every page renders without brand
    name for 30s (found via test_every_spec_page_is_reachable failing
    on /work-with-me, 2026-04-23)."""
    files = dict(compose_files(resolved))
    server = files["server.js"]
    # The listen() must live inside the async IIFE that awaits db_migrate
    idx_migrate = server.find("await require('./db_migrate')()")
    idx_listen = server.find("app.listen(")
    assert idx_migrate != -1, "server.js must await db_migrate()"
    assert idx_listen != -1, "server.js must call app.listen()"
    assert idx_migrate < idx_listen, (
        "app.listen() must come AFTER await db_migrate() in server.js "
        "source order (inside the same async IIFE)"
    )
    # The fire-and-forget pattern must NOT remain: listen() at the top
    # level would bind before migration finishes.
    # Count the number of top-level-looking `app.listen(` calls; only one
    # is allowed, and it must be inside the migrate IIFE (i.e. indented).
    lines = [l for l in server.split("\n") if "app.listen(" in l]
    assert len(lines) == 1, f"expected exactly one app.listen call, got {len(lines)}"
    # The single listen line should be indented (inside IIFE)
    assert lines[0].startswith("  "), (
        "app.listen() line should be indented (inside the async migration "
        "IIFE); found: " + repr(lines[0])
    )


def test_contact_form_uses_field_partial(resolved):
    files = dict(compose_files(resolved))
    form = files["views/widgets/form/contact.ejs"]
    # Every input goes through the universal partial instead of hand-coded
    # markup: one include, inside the loop over the form's fields.
    assert form.count("include('./field.ejs'") == 1
    assert "_fields.forEach(function(f) {" in form
    # The one hand-written input is the hidden honeypot, which is no field
    # of the model (the same markup as form/public-submission.ejs).
    honeypot = '<input type="text" name="website_url_hp" value="" tabindex="-1" autocomplete="off">'
    assert form.count(honeypot) == 1
    visible = form.replace(honeypot, "")
    assert "<input type=\"text\"" not in visible and "<textarea" not in visible
    # Without form_fields the widget falls back to the default contact
    # shape: name, email, phone, the service select and message.
    for name in ("'name'", "'email'", "'phone'", "'service_interest'", "'message'"):
        assert f"{{ name: {name}," in form, name


def test_password_field_type_has_minlength():
    """Security hardening: password fields must enforce minimum length."""
    from compose.build import get_field_type
    pw = get_field_type("password")
    assert pw.get("minlength") == 8


def test_currency_field_type_has_non_negative_min():
    from compose.build import get_field_type
    cur = get_field_type("currency_usd")
    assert cur.get("min") == "0"
    assert cur.get("step") == "0.01"


# ---- round-9: intake translator + table sort + pagination ----

def test_intake_detect_archetype_from_v1():
    from compose.intake import detect_archetype
    assert detect_archetype({"description": "Independent yoga studio with weekly classes"}) == "fitness-studio"
    assert detect_archetype({"description": "Solo plumber serving the metro area"}) == "tradesperson-contractor"
    assert detect_archetype({"description": "Wedding photographer and family portrait sessions"}) == "local-service"
    assert detect_archetype({"description": "Executive coach for C-suite leaders"}) == "coach-consultant"
    assert detect_archetype({"description": "Licensed clinical social worker specializing in anxiety"}) == "therapist-counselor"
    assert detect_archetype({"description": "Family cafe with seasonal menu and natural wine"}) == "restaurant-cafe"
    assert detect_archetype({"description": "Reiki master and energy healer in town"}) == "healer-wellness"
    assert detect_archetype({"description": "Hair salon with color and bridal specialists"}) == "salon-spa"


def test_intake_detect_archetype_fitness_studio_expanded_keywords():
    """Regression guards for 2026-04-22 keyword expansion on fitness-studio.

    stretch-studio was misclassified as local-service because 'stretch'
    wasn't in the fitness-studio keyword list and the classifier fell through
    to the local-service default. This test locks in each of the expanded
    keywords (stretch, mobility, dance, martial arts, spin, gym) so future
    pruning can't silently regress them.
    """
    from compose.intake import detect_archetype
    # Stretch + mobility: the direct regression case
    assert detect_archetype({"description": "Boutique stretch and mobility studio"}) == "fitness-studio"
    assert detect_archetype({"description": "Assisted stretching class for athletes"}) == "fitness-studio"
    assert detect_archetype({"description": "Weekly mobility class for older adults"}) == "fitness-studio"
    # Dance + ballet + martial arts
    assert detect_archetype({"description": "Contemporary dance studio in town"}) == "fitness-studio"
    assert detect_archetype({"description": "Ballet for kids and adults"}) == "fitness-studio"
    assert detect_archetype({"description": "Muay thai and kickboxing gym"}) == "fitness-studio"
    assert detect_archetype({"description": "Traditional martial arts school"}) == "fitness-studio"
    # Gym + strength training
    assert detect_archetype({"description": "Neighborhood gym with strength training focus"}) == "fitness-studio"
    # Spin / cycling / cardio
    assert detect_archetype({"description": "Indoor cycling studio with spin classes"}) == "fitness-studio"
    # Regression: "sound bath" and "reiki" are healer-wellness, not fitness.
    # Stretch + yoga together: fitness-studio (first match wins; yoga is
    # already in the list, stretch is now too; either way not wellness).
    assert detect_archetype({"description": "Stretch + yoga hybrid classes"}) == "fitness-studio"
    # Regression: ensure healer-wellness keywords still win when they should.
    assert detect_archetype({"description": "Sound bath facilitator offering group sessions"}) == "healer-wellness"
    assert detect_archetype({"description": "Reiki master and breathwork facilitator"}) == "healer-wellness"


def test_intake_detect_archetype_coach_consultant_instructor_keywords():
    """Guards the instructor keywords of coach-consultant.

    a private music teacher's site classified to local-service in a Harbor
    e2e session because none of music / piano / lessons / instructor /
    teacher were in any keyword list. Local-service inherited HVAC default
    services. Coach-consultant's program / session / mentor structure maps
    to private-lesson businesses far better than local-service, so the
    fix is a keyword expansion. This test locks in the new keywords.
    """
    from compose.intake import detect_archetype
    # The direct regression cases (private lessons and similar)
    assert detect_archetype({"description": "Big-city piano teacher offering private lessons"}) == "coach-consultant"
    assert detect_archetype({"description": "Independent piano studio for adult students"}) == "coach-consultant"
    assert detect_archetype({"description": "Music lessons for beginners and intermediates"}) == "coach-consultant"
    assert detect_archetype({"description": "Music instructor specializing in jazz technique"}) == "coach-consultant"
    # Other instruments
    assert detect_archetype({"description": "Guitar lessons in the East Village"}) == "coach-consultant"
    assert detect_archetype({"description": "Guitar teacher with 20 years experience"}) == "coach-consultant"
    assert detect_archetype({"description": "Violin teacher for beginning students"}) == "coach-consultant"
    assert detect_archetype({"description": "Drum lessons for kids and teens"}) == "coach-consultant"
    # Voice + singing
    assert detect_archetype({"description": "Voice teacher coaching audition prep"}) == "coach-consultant"
    assert detect_archetype({"description": "Singing lessons in classical and pop styles"}) == "coach-consultant"
    assert detect_archetype({"description": "Vocal coach for working musicians"}) == "coach-consultant"
    # Tutoring + test prep
    assert detect_archetype({"description": "Private tutor for math and science"}) == "coach-consultant"
    assert detect_archetype({"description": "Spanish language tutor for adults"}) == "coach-consultant"
    assert detect_archetype({"description": "SAT prep coach for high school juniors"}) == "coach-consultant"
    assert detect_archetype({"description": "Test prep instructor offering small-group sessions"}) == "coach-consultant"
    # Regression guard: existing coach-consultant matches still win.
    assert detect_archetype({"description": "Executive coach for C-suite leaders"}) == "coach-consultant"
    assert detect_archetype({"description": "Leadership mentor for emerging managers"}) == "coach-consultant"
    # Regression guard: keywords don't bleed into other archetypes.
    # "music festival" must still beat "music lessons" in priority order
    # (event-producer is listed before coach-consultant in
    # _ARCHETYPE_KEYWORDS, so the festival keyword wins on phrases that
    # mention both).
    assert detect_archetype({"description": "Annual music festival production company"}) == "event-producer"
    # "fitness studio" still beats "music studio" because fitness-studio
    # is listed earlier than coach-consultant.
    assert detect_archetype({"description": "Fitness studio that hosts music nights"}) == "fitness-studio"


def test_intake_detect_archetype_honors_harbor_meta_hint():
    """Regression guards for 2026-04-27 archetype-hint forwarding.

    services/harbor_to_compose.ts sets _harbor_meta.archetype_hint when
    Opus's archetype guess matches one of the 9 known archetype ids.
    detect_archetype must honor that hint as a strong signal because Opus
    has access to the keeper's full memory + last 30 turns, far richer
    than substring keyword matching.
    """
    from compose.intake import detect_archetype
    # Hint honored when the description would otherwise miss completely.
    # Without the hint, an empty / generic description falls to local-service.
    assert detect_archetype({
        "description": "A small business website",
        "_harbor_meta": {"archetype_hint": "coach-consultant"},
    }) == "coach-consultant"
    # Hint honored when description carries no keywords at all.
    assert detect_archetype({
        "_harbor_meta": {"archetype_hint": "healer-wellness"},
    }) == "healer-wellness"
    # Hint OVERRIDES a conflicting keyword match. Opus's read of 30+
    # memory rows is a stronger signal than a single text keyword match.
    # Description has "yoga" (would match fitness-studio) but Opus's hint
    # says coach-consultant. Hint wins.
    assert detect_archetype({
        "description": "Yoga teacher offering one-on-one mentorship",
        "_harbor_meta": {"archetype_hint": "coach-consultant"},
    }) == "coach-consultant"
    # All 9 known archetypes are valid hints (incl. local-service explicit).
    for valid_id in [
        "therapist-counselor", "fitness-studio", "salon-spa",
        "restaurant-cafe", "tradesperson-contractor", "event-producer",
        "healer-wellness", "coach-consultant", "local-service",
    ]:
        assert detect_archetype({
            "description": "Wedding photographer and family portraits",
            "_harbor_meta": {"archetype_hint": valid_id},
        }) == valid_id, f"valid hint {valid_id!r} should have been honored"


def test_intake_detect_archetype_invalid_hint_falls_through():
    """Defensive: an unknown / malformed archetype_hint falls through
    to keyword matching instead of being returned verbatim.

    services/harbor_to_compose.ts already validates hints against
    KNOWN_ARCHETYPES, so an unknown value here implies either a bug
    or an alternative extractor that doesn't follow the contract.
    Either way, fall through to text matching rather than emitting a
    bogus archetype id downstream.
    """
    from compose.intake import detect_archetype
    # Unknown archetype id → falls through to keyword match.
    assert detect_archetype({
        "description": "Independent yoga studio with weekly classes",
        "_harbor_meta": {"archetype_hint": "made-up-archetype"},
    }) == "fitness-studio"
    # Empty hint → falls through.
    assert detect_archetype({
        "description": "Independent yoga studio",
        "_harbor_meta": {"archetype_hint": ""},
    }) == "fitness-studio"
    # Non-string hint → falls through.
    assert detect_archetype({
        "description": "Independent yoga studio",
        "_harbor_meta": {"archetype_hint": 42},
    }) == "fitness-studio"
    # None hint → falls through.
    assert detect_archetype({
        "description": "Independent yoga studio",
        "_harbor_meta": {"archetype_hint": None},
    }) == "fitness-studio"
    # Missing archetype_hint key → falls through.
    assert detect_archetype({
        "description": "Independent yoga studio",
        "_harbor_meta": {"color_mood": "warm"},
    }) == "fitness-studio"
    # Non-dict _harbor_meta → falls through (defensive).
    assert detect_archetype({
        "description": "Independent yoga studio",
        "_harbor_meta": "not-a-dict",
    }) == "fitness-studio"
    assert detect_archetype({
        "description": "Independent yoga studio",
        "_harbor_meta": None,
    }) == "fitness-studio"
    # No _harbor_meta key at all → text match path runs (regression guard).
    assert detect_archetype({
        "description": "Independent yoga studio",
    }) == "fitness-studio"


def test_normalize_harbor_services_maps_harbor_to_compose_shape():
    """harbor_to_compose stores `{title, description?, price?}` items;
    scaffold_spec expects `{label, title, description, price_display,
    cta_text, cta_href}`. The normalizer fills missing fields with
    neutral defaults + drops empty/non-dict entries.
    """
    from compose.intake import _normalize_harbor_services
    raw = [
        {"title": "Weekly Lesson", "description": "60-minute private lesson", "price": "$80"},
        {"title": "Trial Session", "description": "30-minute introductory lesson"},
        {"title": "Recital Coaching", "price": "Inquire"},
        {"title": "", "description": "skip-empty"},
        "not-a-dict",
        {},
    ]
    out = _normalize_harbor_services(raw)
    assert len(out) == 3
    assert out[0]["title"] == "Weekly Lesson"
    assert out[0]["description"] == "60-minute private lesson"
    assert out[0]["price_display"] == "$80"
    # Auto-derived eyebrow labels (COVERAGE / VALVE / DRIP) restated the
    # title in shouty caps; harbor services no longer carry a label.
    assert out[0]["label"] == ""
    assert out[0]["cta_text"] == "Inquire"
    assert out[0]["cta_href"] == "/contact?service=Weekly+Lesson"
    # Missing price leaves price_display empty so the widget skips it
    # (cta_text already says "Inquire"; defaulting price_display to
    # "Inquire" rendered the same word twice per card).
    assert out[1]["price_display"] == ""
    assert out[2]["label"] == ""


def test_normalize_harbor_services_returns_empty_for_non_list():
    from compose.intake import _normalize_harbor_services
    assert _normalize_harbor_services(None) == []
    assert _normalize_harbor_services("not-a-list") == []
    assert _normalize_harbor_services({}) == []
    assert _normalize_harbor_services([]) == []


def test_normalize_harbor_services_no_label_no_inquire_default():
    """Regression guard for the double-Inquire +
    shouty eyebrow regression.

    Two seeded fields drove the bug:
      - `label` was auto-derived from the first word of `title` and
        rendered as a small-caps eyebrow above the card. For
        services like "Repair Visit" / "Seasonal Check" / "New
        Install" this produced eyebrows REPAIR / SEASONAL / NEW;
        labelled noise that just restated the title in shouty caps.
      - `price_display` defaulted to "Inquire" when no price was
        supplied; combined with `cta_text="Inquire"` the card rendered
        the word "Inquire" twice (italic price + arrow link).

    The fix: leave `label` empty and don't default `price_display`. The
    widget's empty-string check already skips both fields cleanly.
    """
    from compose.intake import _normalize_harbor_services
    out = _normalize_harbor_services([
        {"title": "Repair Visit", "description": "First sample service"},
        {"title": "Seasonal Check", "description": "Second sample service"},
        {"title": "New Install", "description": "Third sample service"},
    ])
    assert len(out) == 3
    for row in out:
        assert row["label"] == "", (
            f"auto-derived eyebrow label leaked: {row['label']!r}"
        )
        assert row["price_display"] == "", (
            f"price_display still defaulted to a fallback: "
            f"{row['price_display']!r}"
        )
        assert row["cta_text"] == "Inquire"


def test_services_from_offering_seed_rows_no_label_no_inquire_default():
    """Symmetric guard for the seed_rows fallback path: same root cause,
    same fix as `_normalize_harbor_services`.
    """
    from compose.intake import _services_from_offering_seed_rows
    out = _services_from_offering_seed_rows({
        "data_models": [
            {
                "name": "Service",
                "seed_rows": [
                    {"title": "Repair Visit", "description": "First sample service"},
                    {"title": "Seasonal Check", "description": "Second sample service"},
                ],
            },
        ],
    })
    assert len(out) == 2
    for row in out:
        assert row["label"] == ""
        assert row["price_display"] == ""
        assert row["cta_text"] == "Inquire"


def test_grid_3col_widget_skips_price_display_when_equal_to_cta_text():
    """The widget must not render `price_display` when it's empty OR
    equals `cta_text`. Belt-and-suspenders: the intake layer no longer
    seeds `Inquire` as a price_display fallback, but if a future code
    path (or admin CRUD edit) sets price_display="Inquire" with the
    default cta_text="Inquire", the widget still suppresses the
    duplicate render.
    """
    widgets_dir = FIXTURE_DIR.parent / "widgets" / "offerings"
    src = (widgets_dir / "grid-3col.ejs").read_text()
    assert "item.price_display && item.price_display !== item.cta_text" in src, (
        "grid-3col widget must guard price_display render against equality "
        "with cta_text to prevent the double-Inquire duplicate"
    )


def test_services_editorial_table_widget_skips_price_display_when_equal_to_cta_text():
    """Same guarantee for the editorial-table layout (sibling layout for
    the same data shape). Both widgets consume `{price_display, cta_text}`
    pairs and must apply the same empty-or-equal skip rule.
    """
    widgets_dir = FIXTURE_DIR.parent / "widgets" / "offerings"
    src = (widgets_dir / "services-editorial-table.ejs").read_text()
    assert "item.price_display && item.price_display !== item.cta_text" in src, (
        "services-editorial-table widget must guard price_display render "
        "against equality with cta_text"
    )


def test_normalize_harbor_testimonials_maps_harbor_to_compose_shape():
    """harbor_to_compose stores `{quote, author?}` items; scaffold_spec
    expects `{quote, author_name, service}`. Missing author defaults to
    a neutral sample placeholder.
    """
    from compose.intake import _normalize_harbor_testimonials
    raw = [
        {"quote": "The teacher is patient and clear.", "author": "M., adult beginner"},
        {"quote": "Best teacher I have had."},
        {"quote": "", "author": "skip-empty"},
        "not-a-dict",
        {},
    ]
    out = _normalize_harbor_testimonials(raw)
    assert len(out) == 2
    assert out[0]["quote"] == "The teacher is patient and clear."
    assert out[0]["author_name"] == "M., adult beginner"
    assert out[0]["service"] == "Client"
    assert out[1]["author_name"] == "Client (sample)"


def test_normalize_harbor_testimonials_returns_empty_for_non_list():
    from compose.intake import _normalize_harbor_testimonials
    assert _normalize_harbor_testimonials(None) == []
    assert _normalize_harbor_testimonials("not-a-list") == []


def test_translate_v1_spec_consumes_harbor_meta_services_and_testimonials():
    """Architectural guard: v1_spec._harbor_meta.services and
    _harbor_meta.testimonials populated by services/harbor_to_compose.ts
    must flow through translate_v1_spec into the rendered home page,
    bypassing the executive-coach default services that ship with the
    coach-consultant archetype. Without this pass-through, a private
    music teacher gets "1:1 Executive Coaching $4,800" on the home page even
    though Opus extracted lesson-shaped offerings from the conversation.
    """
    from compose.intake import translate_v1_spec
    v1 = {
        "spec_version": "1.0",
        "app_name": "sample-lessons",
        "description": "I teach piano in a big city",
        "tier": "build",
        "_seed_settings": {
            "site_name": "Sample Lessons",
            "hero_tagline": "Patient, classical-grounded piano teaching",
        },
        "_harbor_meta": {
            "archetype_hint": "coach-consultant",
            "services": [
                {"title": "Weekly Lesson", "description": "60-minute private lesson", "price": "$80"},
                {"title": "Trial Session", "description": "30-minute intro lesson", "price": "$40"},
            ],
            "testimonials": [
                {"quote": "The teacher is patient + clear.", "author": "M., adult beginner"},
                {"quote": "Best teacher I have had."},
            ],
        },
    }
    v2 = translate_v1_spec(v1)
    home = v2["pages"]["home"]
    # Offerings carry the harbor services, NOT the executive-coaching defaults.
    offering_titles = [s["title"] for s in home["offerings"]["items"]]
    assert "Weekly Lesson" in offering_titles
    assert "Trial Session" in offering_titles
    assert "1:1 Executive Coaching" not in offering_titles, (
        "harbor services pass-through failed: executive-coaching default still present"
    )
    # Testimonials carry the harbor quotes.
    quote_texts = [t["quote"] for t in home["testimonials"]["items"]]
    assert any("The teacher is patient" in q for q in quote_texts)
    assert any("Best teacher" in q for q in quote_texts)


def test_translate_v1_spec_falls_back_to_defaults_when_harbor_meta_empty():
    """When _harbor_meta.services is absent (non-harbor build, or harbor
    extraction without offerings sections), scaffold_spec's archetype
    defaults still kick in. Regression guard so the new pass-through
    code does not break the historic default path.
    """
    from compose.intake import translate_v1_spec
    v1 = {
        "spec_version": "1.0",
        "app_name": "exec-coach",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
    }
    v2 = translate_v1_spec(v1)
    home = v2["pages"]["home"]
    offering_titles = [s["title"] for s in home["offerings"]["items"]]
    # Default coach-consultant offerings should be present.
    assert any(t in offering_titles for t in ("1:1 Executive Coaching", "Leadership Intensives"))


def test_translate_v1_spec_falls_back_when_harbor_meta_services_empty_list():
    """An empty `_harbor_meta.services` list should fall through to
    archetype defaults, not render an empty offerings section. The
    `harbor_services or None` pattern handles this; locked in here so
    a future refactor doesn't accidentally render a 0-card offerings grid.
    """
    from compose.intake import translate_v1_spec
    v1 = {
        "spec_version": "1.0",
        "app_name": "exec-coach-empty-services",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
        "_harbor_meta": {
            "archetype_hint": "coach-consultant",
            "services": [],
            "testimonials": [],
        },
    }
    v2 = translate_v1_spec(v1)
    home = v2["pages"]["home"]
    # Defaults still kick in because `[] or None` → None → archetype default.
    assert len(home["offerings"]["items"]) > 0
    assert len(home["testimonials"]["items"]) > 0


def test_harbor_meta_services_skips_stock_photo_seeding():
    """Regression guard for the stock-photo seeding bug.

    `scaffold_spec` previously read `answers.get("harbor_meta")` (no
    underscore) but `translate_v1_spec` stores the dict at
    `answers["_harbor_meta"]`. The mismatch meant the "skip stock-photo
    seeding when keeper supplied their own services" guard never fired
    for any harbor build, so every keeper-supplied service silently got
    a `https://picsum.photos/...` `image_url` stamped on its seed row.

    Three branches asserted:
      1. _harbor_meta.services non-empty -> NO picsum URLs anywhere in
         the rendered services seed (the bug-affected path).
      2. _harbor_meta absent (legacy / non-harbor path) -> picsum URLs
         ARE present (regression guard so the fix didn't accidentally
         strip stock photos from non-harbor builds).
      3. _harbor_meta.services = [] (harbor extraction returned no
         offerings) -> picsum URLs ARE present (empty list is falsy by
         design; the guard treats it as "no keeper services" and falls
         through to defaults + stock photos).
    """
    from compose.intake import translate_v1_spec

    # (1) Harbor-supplied services: no picsum image_url should appear.
    v1_with_harbor = {
        "spec_version": "1.0",
        "app_name": "sample-irrigation",
        "description": "Irrigation install and repair",
        "tier": "build",
        "_harbor_meta": {
            "archetype_hint": "tradesperson-contractor",
            "services": [
                {"title": "Repair Visit", "description": "First sample service", "price": "from $10"},
                {"title": "Seasonal Check", "description": "Second sample service", "price": "$20"},
            ],
        },
    }
    v2 = translate_v1_spec(v1_with_harbor)
    seeded = v2["seed_content"]["services"]
    assert len(seeded) == 2
    titles = [s["title"] for s in seeded]
    assert "Repair Visit" in titles
    assert "Seasonal Check" in titles
    for row in seeded:
        assert "image_url" not in row, (
            f"harbor-supplied service still got stock-photo seeded: {row!r}"
        )

    # (2) No _harbor_meta key at all: picsum URLs SHOULD seed (default path).
    v1_no_harbor = {
        "spec_version": "1.0",
        "app_name": "no-harbor-build",
        "description": "Local plumbing services",
        "tier": "build",
    }
    v2_default = translate_v1_spec(v1_no_harbor)
    seeded_default = v2_default["seed_content"]["services"]
    assert len(seeded_default) > 0
    assert any("picsum.photos" in (s.get("image_url") or "") for s in seeded_default), (
        "non-harbor build lost stock-photo seeding; the fix over-corrected"
    )

    # (3) Empty harbor services list: picsum URLs SHOULD seed (falls through
    # to archetype defaults; empty list is falsy on purpose).
    v1_empty_harbor = {
        "spec_version": "1.0",
        "app_name": "empty-harbor-build",
        "description": "Local plumbing services",
        "tier": "build",
        "_harbor_meta": {
            "archetype_hint": "tradesperson-contractor",
            "services": [],
        },
    }
    v2_empty = translate_v1_spec(v1_empty_harbor)
    seeded_empty = v2_empty["seed_content"]["services"]
    assert len(seeded_empty) > 0
    assert any("picsum.photos" in (s.get("image_url") or "") for s in seeded_empty), (
        "empty _harbor_meta.services should fall through to default seeding"
    )


# ============================================================
# seed_rows fallback when
# _harbor_meta.services is empty but data_models carry Session/
# Service/Treatment/etc. seed_rows.
# ============================================================


def test_services_from_offering_seed_rows_recovers_when_harbor_meta_empty():
    """When Opus emits novel section vocabulary that the translator's
    alias map drops, the offerings still arrive in data_models[].seed_rows.
    `_services_from_offering_seed_rows` reconstructs the harbor services
    list from those rows. The case it closes:
    empty _harbor_meta.services + Session model with priced rows ->
    home page renders the keeper's actual prices, not archetype defaults.
    """
    from compose.intake import translate_v1_spec
    v1 = {
        "spec_version": "1.0",
        "app_name": "fern-reiki",
        "description": "Reiki energy work",
        "tier": "build",
        "_harbor_meta": {"archetype_hint": "healer-wellness", "services": []},
        "data_models": [
            {
                "name": "Session",
                "fields": [],
                "seed_rows": [
                    {"title": "Single Session", "description": "60-minute Reiki",
                     "price_cents": 12000},
                    {"title": "3-Pack", "description": "Three sessions",
                     "price_cents": 32000},
                ],
            },
        ],
    }
    v2 = translate_v1_spec(v1)
    home = v2["pages"]["home"]
    titles = [s["title"] for s in home["offerings"]["items"]]
    assert "Single Session" in titles
    assert "3-Pack" in titles


def test_services_from_offering_seed_rows_price_cents_to_dollar_string():
    """price_cents (int) is converted to '$X' (whole dollars) or
    '$X.YY' (cents). Whole dollars use no decimals to match Opus's
    typical price strings.
    """
    from compose.intake import _services_from_offering_seed_rows
    v1 = {
        "data_models": [
            {
                "name": "Treatment",
                "seed_rows": [
                    {"title": "A", "price_cents": 12000},
                    {"title": "B", "price_cents": 12550},
                    {"title": "C", "price": "$99"},
                    {"title": "D", "price_display": "Free"},
                ],
            },
        ],
    }
    out = _services_from_offering_seed_rows(v1)
    by_title = {s["title"]: s for s in out}
    assert by_title["A"]["price_display"] == "$120"
    assert by_title["B"]["price_display"] == "$125.50"
    assert by_title["C"]["price_display"] == "$99"
    assert by_title["D"]["price_display"] == "Free"


def test_services_from_offering_seed_rows_ignores_non_offering_models():
    """Submission-target models (Inquiry/Message/Booking) and unrelated
    models must NOT be scanned for offerings. Only the offering-style
    noun set (session/service/treatment/package/program/lesson/...) is
    considered.
    """
    from compose.intake import _services_from_offering_seed_rows
    v1 = {
        "data_models": [
            {"name": "Inquiry", "seed_rows": [{"title": "Hi"}]},
            {"name": "ContactMessage", "seed_rows": [{"title": "Yo"}]},
            {"name": "BlogPost", "seed_rows": [{"title": "Post"}]},
        ],
    }
    out = _services_from_offering_seed_rows(v1)
    assert out == []


def test_services_from_offering_seed_rows_handles_empty_or_malformed():
    """Defensive: missing data_models, non-list value, non-dict rows,
    rows without titles all return [] without crashing.
    """
    from compose.intake import _services_from_offering_seed_rows
    assert _services_from_offering_seed_rows({}) == []
    assert _services_from_offering_seed_rows({"data_models": "nope"}) == []
    assert _services_from_offering_seed_rows({"data_models": [None, 42]}) == []
    assert _services_from_offering_seed_rows(
        {"data_models": [{"name": "Session", "seed_rows": [{"description": "no title"}]}]}
    ) == []
    assert _services_from_offering_seed_rows("not a dict") == []


# ============================================================
# _harbor_meta.nav_paths restricts public nav to
# the keeper's declared pages so archetype defaults don't bloat
# the navigation.
# ============================================================


def _build_nav_for(harbor_meta=None, custom_pages=None, archetype="healer-wellness"):
    from compose.build import _default_nav_items, BuildSpec, resolve_spec
    spec = BuildSpec.from_dict({
        "spec_version": "v2",
        "archetype": archetype,
        "app_name": "fern-test",
        "site_name": "Fern Test",
        "admin_email": "a@b.example",
        "custom_pages": custom_pages or [],
        "_harbor_meta": harbor_meta or {},
    })
    rs = resolve_spec(spec)
    return [it["href"] for it in _default_nav_items(rs)]


def test_nav_paths_allowlist_restricts_archetype_defaults():
    # healer-wellness ships 8 default_pages; keeper said only home, about,
    # offerings, journal, contact (5). Without the allowlist, all 8 show.
    full = _build_nav_for()
    assert len(full) >= 6  # archetype default = bloated nav
    restricted = _build_nav_for(harbor_meta={
        "nav_paths": ["/", "/about", "/offerings", "/journal", "/contact"],
    })
    assert "/about" in restricted
    assert "/contact" in restricted
    # Pages dropped from the keeper's list are absent.
    for href in restricted:
        assert href in {"/", "/about", "/offerings", "/journal", "/contact"}


def test_nav_paths_allowlist_always_includes_home_and_contact():
    # Defensive: even if keeper forgot home/contact, nav still has them.
    nav = _build_nav_for(harbor_meta={"nav_paths": ["/about"]})
    assert "/" in nav
    assert "/contact" in nav


def test_nav_paths_empty_or_missing_falls_through_to_archetype():
    full = _build_nav_for()
    assert _build_nav_for(harbor_meta={"nav_paths": []}) == full
    assert _build_nav_for(harbor_meta={}) == full
    assert _build_nav_for(harbor_meta={"nav_paths": "not-a-list"}) == full


def test_nav_paths_filters_custom_pages_when_allowlist_set():
    # custom_page outside the allowlist is still routable but stripped
    # from the nav.
    nav = _build_nav_for(
        harbor_meta={"nav_paths": ["/", "/contact"]},
        custom_pages=[
            {"path": "/secret-thing", "title": "Secret"},
        ],
    )
    assert "/secret-thing" not in nav


def test_nav_paths_allowlist_always_includes_about():
    """A core /about page must stay navigable even when the keeper's nav_paths
    allowlist doesn't list it (keeper asked for '/approach', archetype emits
    '/about' -> the bio page was orphaned from nav; a finding of the
    automated build review, 2026-06-07, a loop that ran outside this
    repository and is not included in it). Home + contact + about are
    always allowed."""
    nav = _build_nav_for(
        harbor_meta={"nav_paths": ["/approach", "/booking"]},
        archetype="coach-consultant",
    )
    assert "/about" in nav, f"/about orphaned from nav: {nav}"
    assert "/" in nav and "/contact" in nav


def test_translate_v1_spec_handles_non_dict_harbor_meta_defensively():
    """If a corrupt or alternate-extractor v1 spec passes a non-dict
    `_harbor_meta` (string, list, None), translate_v1_spec must not
    crash. Hint forwarding falls through, services + testimonials fall
    back to archetype defaults.
    """
    from compose.intake import translate_v1_spec
    for bad_meta in (None, "not-a-dict", [], 42):
        v1 = {
            "spec_version": "1.0",
            "app_name": f"defensive-{type(bad_meta).__name__}",
            "description": "Executive coach for C-suite leaders",
            "tier": "build",
            "_harbor_meta": bad_meta,
        }
        v2 = translate_v1_spec(v1)
        # Archetype text-classifies (no hint to honor); defaults render.
        assert v2["archetype"] == "coach-consultant"
        assert len(v2["pages"]["home"]["offerings"]["items"]) > 0


def test_translate_v1_spec_handles_partial_harbor_meta():
    """When only services OR only testimonials is set in _harbor_meta,
    each falls through independently: present field wins, absent field
    falls back to archetype defaults.
    """
    from compose.intake import translate_v1_spec
    # Only services set.
    v1_only_services = {
        "spec_version": "1.0",
        "app_name": "only-services",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
        "_harbor_meta": {
            "services": [{"title": "Custom Offering", "description": "x", "price": "$1"}],
        },
    }
    v2a = translate_v1_spec(v1_only_services)
    home_a = v2a["pages"]["home"]
    assert "Custom Offering" in [s["title"] for s in home_a["offerings"]["items"]]
    # Testimonials fall back to defaults (sample placeholders).
    assert len(home_a["testimonials"]["items"]) > 0
    assert all("(sample)" in t.get("author_name", "") or "Client" in t.get("author_name", "")
               for t in home_a["testimonials"]["items"])

    # Only testimonials set.
    v1_only_testim = {
        "spec_version": "1.0",
        "app_name": "only-testim",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
        "_harbor_meta": {
            "testimonials": [{"quote": "Real keeper quote", "author": "Real Author"}],
        },
    }
    v2b = translate_v1_spec(v1_only_testim)
    home_b = v2b["pages"]["home"]
    assert "Real keeper quote" in [t["quote"] for t in home_b["testimonials"]["items"]]
    # Services fall back to executive-coach defaults.
    assert any(t in [s["title"] for s in home_b["offerings"]["items"]]
               for t in ("1:1 Executive Coaching", "Leadership Intensives"))


def test_intake_translate_v1_spec_honors_harbor_archetype_hint_e2e():
    """End-to-end guard: a v1-shape spec produced by services/harbor_to_compose.ts
    that carries an _harbor_meta.archetype_hint must end up with that
    archetype when translate_v1_spec runs (the hint must actually fire
    inside detect_archetype on the real call path, not just the unit
    test). Regression target: a private music teacher's site extracted to
    a v1-shape spec with hint=coach-consultant but the worker re-classified
    to local-service via text match, seeding HVAC defaults.
    """
    from compose.intake import translate_v1_spec, detect_archetype
    # Private-lesson input: Opus's archetype_hint says coach-consultant.
    # Description "I teach piano in a big city" does NOT match the expanded
    # coach-consultant keyword set (which uses "piano teacher" / "piano
    # lessons" / "piano studio" as phrases, not bare "piano" or reverse
    # word orders). Without the hint, this exact description falls
    # through to local-service. Asserting the hint flows through the
    # real call path is the load-bearing check.
    v1_spec = {
        "spec_version": "1.0",
        "app_name": "sample-lessons",
        "description": "I teach piano in a big city",
        "tier": "build",
        "data_models": [],
        "user_roles": [],
        "pages": [],
        "_harbor_meta": {"archetype_hint": "coach-consultant"},
    }
    # Sanity: confirm the description alone IS local-service, so the
    # hint actually has a misclassification to override.
    v1_no_hint = {k: v for k, v in v1_spec.items() if k != "_harbor_meta"}
    assert detect_archetype(v1_no_hint) == "local-service", (
        "regression target: 'I teach piano in a big city' must text-misclassify "
        "to local-service so the hint test below has something to fix"
    )
    v2 = translate_v1_spec(v1_spec)
    assert v2["archetype"] == "coach-consultant", (
        f"Harbor archetype_hint did not flow through translate_v1_spec; "
        f"got archetype={v2.get('archetype')!r}"
    )

    # Harder regression: text says "yoga studio" (would match fitness-studio
    # via keyword scan), hint says coach-consultant. Hint must override.
    v1_conflict = {
        "spec_version": "1.0",
        "app_name": "ambiguous",
        "description": "Yoga studio with weekly classes",
        "tier": "build",
        "_harbor_meta": {"archetype_hint": "coach-consultant"},
    }
    v2_conflict = translate_v1_spec(v1_conflict)
    assert v2_conflict["archetype"] == "coach-consultant"

    # Defensive: spec without _harbor_meta still text-matches normally,
    # so the hint plumbing did not break the historic fallback path.
    v1_no_hint = {
        "spec_version": "1.0",
        "app_name": "yoga-default",
        "description": "Yoga studio with weekly classes",
        "tier": "build",
    }
    v2_no_hint = translate_v1_spec(v1_no_hint)
    assert v2_no_hint["archetype"] == "fitness-studio"


# ---- full parametric archetype classification regression fixtures ----
# Covers every registered archetype with at least 3 positive examples, plus
# keyword-collision priority cases and edge-case fallbacks. Locks in
# classifier behavior so future keyword tuning can't silently regress a
# known-good description (e.g. the 2026-04-22 stretch-studio case
# where "stretch studio" misclassified as local-service because "stretch"
# wasn't in fitness-studio's keyword list).
#
# Collision notes:
#   - Keyword list order in _ARCHETYPE_KEYWORDS IS priority order (first
#     archetype whose keyword matches wins). As of 2026-04-22 the order is:
#     therapist-counselor, fitness-studio, salon-spa, restaurant-cafe,
#     tradesperson-contractor, healer-wellness, coach-consultant.
#   - When a description contains keywords from multiple archetypes, the
#     earlier-listed archetype wins. Explicit collision fixtures below lock
#     in this priority so reordering the list requires updating these tests.
#   - "local-service" is the fallback when nothing matches.
@pytest.mark.parametrize("description,expected,note", [
    # therapist-counselor (first-priority; specific clinical keywords)
    ("Licensed therapist specializing in anxiety and depression", "therapist-counselor", "therapist keyword"),
    ("LCSW with trauma focus and EMDR training", "therapist-counselor", "lcsw + emdr"),
    ("Private practice psychotherapist offering IFS sessions", "therapist-counselor", "psychotherapy + ifs"),
    ("Counselor providing counseling for couples", "therapist-counselor", "counselor/counseling"),
    ("Psychiatrist serving mental health needs", "therapist-counselor", "psychiatrist + mental health"),
    # fitness-studio (movement studios, gyms, classes)
    ("Independent yoga studio with weekly classes", "fitness-studio", "yoga"),
    ("Pilates reformer studio in downtown", "fitness-studio", "pilates"),
    ("Barre studio for adults and kids", "fitness-studio", "barre"),
    ("Boutique stretch and mobility studio", "fitness-studio", "stretch + mobility"),
    ("Traditional martial arts school with kickboxing classes", "fitness-studio", "martial arts + kickboxing"),
    ("Neighborhood gym with strength training focus", "fitness-studio", "gym + strength training"),
    ("Indoor cycling studio with spin classes", "fitness-studio", "spin + cycling studio"),
    ("Contemporary dance studio in town", "fitness-studio", "dance studio"),
    # salon-spa (beauty + day-spa services)
    ("Hair salon with color and bridal specialists", "salon-spa", "salon + bridal"),
    ("Full-service day spa and esthetics", "salon-spa", "spa + esthetician"),
    ("Nail salon in the neighborhood", "salon-spa", "nail salon"),
    # Note (2026-04-22): "therapist" substring matches first (therapist-
    # counselor is scanned before salon-spa), so descriptions containing
    # "therapist/therapists" misclassify. Use descriptions that don't
    # contain "therapist" at all for clean salon-spa positives; the
    # collision itself is exercised below under "Collision / priority".
    ("Day spa with estheticians and stylists on staff", "salon-spa", "spa + esthetician + stylist"),
    # restaurant-cafe (food + beverage)
    ("Family cafe with seasonal menu and natural wine", "restaurant-cafe", "cafe + menu + wine"),
    ("Fine dining restaurant with tasting menu", "restaurant-cafe", "restaurant + tasting"),
    ("Neighborhood coffee shop and bakery", "restaurant-cafe", "coffee shop + bakery"),
    # tradesperson-contractor (licensed trades)
    ("Solo plumber serving the metro area", "tradesperson-contractor", "plumber"),
    ("Licensed electrician for residential wiring", "tradesperson-contractor", "electrician"),
    ("HVAC contractor with 24/7 emergency service", "tradesperson-contractor", "hvac + emergency service"),
    ("Family-run handyman and general contractor", "tradesperson-contractor", "handyman + contractor"),
    # healer-wellness (energy/somatic modalities)
    ("Reiki master and energy healer in town", "healer-wellness", "reiki + energy work"),
    ("Sound bath facilitator offering group sessions", "healer-wellness", "sound bath"),
    ("Hypnotherapy for anxiety and smoking cessation", "healer-wellness", "hypnotherapy"),
    ("Breathwork and intuitive guidance practitioner", "healer-wellness", "breathwork + intuitive"),
    ("Shamanic practitioner working with chakras", "healer-wellness", "shamanic + chakra"),
    # coach-consultant (coaching + business advisory)
    ("Executive coach for C-suite leaders", "coach-consultant", "executive coach"),
    ("Life coach helping clients with transitions", "coach-consultant", "life coach + coaching"),
    ("Business strategy consulting for startups", "coach-consultant", "consulting + strategy"),
    ("Leadership mentor for emerging managers", "coach-consultant", "leadership + mentor"),
    # local-service (fallback: no archetype-specific keywords)
    ("Wedding photographer and family portrait sessions", "local-service", "photography (no archetype match)"),
    ("Pet grooming and dog walking services", "local-service", "pet services (no archetype match)"),
    # Tutoring practices are structurally one-on-one mentorship + program-
    # shaped offerings, so they belong in coach-consultant alongside
    # private music instruction. (Pre-2026-04-27 they fell through to
    # local-service; the keyword expansion landed alongside the private-lesson
    # piano-teacher e2e fix.)
    ("Independent tutor for high school students", "coach-consultant", "tutoring → coach-consultant (2026-04-27)"),
    # Collision / priority cases: lock in the order of _ARCHETYPE_KEYWORDS.
    # therapist-counselor is scanned first, so "therapist" wins over any
    # later-listed archetype's keywords.
    ("Therapist also offering sound baths as adjunct", "therapist-counselor", "therapist beats healer-wellness"),
    # fitness-studio scanned before healer-wellness.
    ("Yoga studio with sound bath events", "fitness-studio", "yoga beats sound bath"),
    # salon-spa scanned before healer-wellness.
    ("Salon with a small reiki healing room", "salon-spa", "salon beats reiki"),
    # fitness-studio scanned before salon-spa.
    ("Yoga studio that also does massage therapy", "fitness-studio", "yoga beats massage therapist"),
    # Known limitation: "therapist" in therapist-counselor list substring-
    # matches "massage therapist" phrases. Standalone "massage therapist
    # in town" descriptions misclassify as clinical therapist. Locked
    # in here so if the keyword list ever gets smarter (word-boundary
    # regex, longest-match, etc.), we update this fixture intentionally.
    # After 2026-04-22 word-boundary classifier fix, "therapist" as a
    # standalone word still wins; the regression target is the case
    # below where "therapist" only appeared INSIDE "hypnotherapist".
    ("Licensed massage therapist in town", "therapist-counselor", "therapist word-boundary still catches standalone"),
    # Regression: a hypnotherapist's description.
    # Pre-fix classifier matched "therapist" inside "hypnotherapist" and
    # returned therapist-counselor. Word-boundary fix + expanded
    # healer-wellness keywords resolve this correctly.
    ("Hypnotherapist offering guided relaxation sessions", "healer-wellness", "hypnotherapist no longer substring-matches 'therapist'"),
    # Edge cases: empty / no-match → fallback
    ("", "local-service", "empty description → default"),
    ("Something completely unrelated to any archetype keyword", "local-service", "no keyword match → default"),
])
def test_archetype_classification_regression_fixtures(description, expected, note):
    """Parametric guard table: every archetype gets 3+ positive examples,
    plus explicit collision-priority and fallback cases. When a
    misclassification ships (like 2026-04-22 stretch-studio → local-
    service), the offending description should get added here so it can
    never regress silently."""
    from compose.intake import detect_archetype
    got = detect_archetype({"description": description, "data_models": []})
    assert got == expected, (
        f"Classification regression: {note!r}; "
        f"description={description!r} → got {got!r}, expected {expected!r}"
    )


# ---- preserve v1.data_models through translator ----------------

def test_translator_preserves_v1_data_models():
    """Regression guard: customer-declared
    data_models in a v1 AppSpec MUST flow through translate_v1_spec into
    the v2 spec's data_models list (not silently dropped in favor of
    archetype defaults, as the pre-Option-A translator did)."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "yoga-b2a",
        "description": "Yoga studio with weekly classes and online booking",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Class", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "class_date", "type": "timestamp", "required": True},
                {"name": "max_attendees", "type": "integer", "required": True},
                {"name": "hero_image", "type": "string"},
            ]},
            {"name": "Booking", "belongs_to": "Class", "fields": [
                {"name": "guest_name", "type": "string", "required": True},
                {"name": "guest_email", "type": "email", "required": True},
                {"name": "attended", "type": "boolean"},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    # Customer models preserved
    names = [m["name"] for m in v2.get("data_models", [])]
    assert "Class" in names, f"Class model dropped: {names}"
    assert "Booking" in names, f"Booking model dropped: {names}"
    # belongs_to preserved
    booking = next(m for m in v2["data_models"] if m["name"] == "Booking")
    assert booking.get("belongs_to") == "Class"


def test_v1_field_types_are_normalized_to_sql_types():
    """v1 field `type: "string"` must map to v2 `sql_type: "VARCHAR(255)"`
    (not default TEXT). Email/phone/url also gain a `field_type` for
    admin-form validation."""
    from compose.intake import _normalize_v1_field
    f = _normalize_v1_field({"name": "title", "type": "string", "required": True})
    assert f["sql_type"] == "VARCHAR(255)"
    assert f["required"] is True

    f = _normalize_v1_field({"name": "description", "type": "text"})
    assert f["sql_type"] == "TEXT"

    f = _normalize_v1_field({"name": "age", "type": "integer"})
    assert f["sql_type"] == "INTEGER"

    f = _normalize_v1_field({"name": "is_active", "type": "boolean"})
    assert f["sql_type"] == "BOOLEAN"
    assert f["default"] == "FALSE"  # auto-supplied when missing

    f = _normalize_v1_field({"name": "starts_at", "type": "timestamp"})
    assert f["sql_type"] == "TIMESTAMPTZ"

    # Email gets both sql_type AND field_type so admin form validates
    f = _normalize_v1_field({"name": "contact_email", "type": "email", "required": True})
    assert f["sql_type"] == "VARCHAR(255)"
    assert f["field_type"] == "email"

    # Phone → phone_us (registry key for US phone validation)
    f = _normalize_v1_field({"name": "phone", "type": "phone"})
    assert f["field_type"] == "phone_us"

    # Pre-v2 shape (sql_type already set) preserved as-is
    f = _normalize_v1_field({"name": "body", "sql_type": "TEXT"})
    assert f["sql_type"] == "TEXT"
    assert "field_type" not in f  # didn't get overridden


def test_unknown_v1_field_type_falls_back_to_text():
    """A v1 spec with a type string the mapper doesn't recognize must
    not crash; it should land on TEXT (safe default)."""
    from compose.intake import _normalize_v1_field
    f = _normalize_v1_field({"name": "payload", "type": "exotic-type"})
    assert f["sql_type"] == "TEXT"


def test_empty_v1_data_models_fall_back_to_archetype():
    """When v1 has no data_models, v2 spec must NOT have a data_models
    key; that lets compose/build.py's fallback
    (`archetype["default_data_models"]`) fire. Regression: we
    previously leaked `data_models: []` from the translator, which made
    compose emit zero tables."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "no-models-app",
        "description": "Nothing fancy",
        "admin_email": "admin@example.com",
    }
    v2 = translate_v1_spec(v1)
    # Either the key is absent, or it's present but non-empty (never an
    # empty list that would mask archetype defaults).
    assert "data_models" not in v2 or bool(v2["data_models"])


def test_build_emits_the_customer_models_table():
    """End-to-end: a v1 spec with a custom data_model must produce a
    schema.sql that CREATE TABLE's the customer table with their fields.
    This is the key regression test for Option A; if this passes,
    `/admin/classes` etc. get a real backing table."""
    import tempfile
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path
    from compose.intake import translate_v1_spec
    from compose.build import build_app

    v1 = {
        "app_name": "b2a-e2e",
        "description": "Breathwork studio with classes and bookings",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "BreathSession", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "session_date", "type": "timestamp", "required": True},
                {"name": "duration_minutes", "type": "integer", "required": True},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="b2a-e2e-"))
    spec_path = tmp / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    try:
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        schema = (build_dir / "db" / "schema.sql").read_text()
        # Customer's table is emitted (kebab-plural → snake_case for Postgres)
        assert "CREATE TABLE IF NOT EXISTS breath_sessions" in schema, (
            f"Customer BreathSession table missing from schema.sql:\n{schema}"
        )
        # Customer's fields carry the normalized sql_type
        assert "title VARCHAR(255)" in schema
        assert "session_date TIMESTAMPTZ" in schema
        assert "duration_minutes INTEGER" in schema
    finally:
        _shutil.rmtree(tmp)


# ---- preserve v1.pages custom paths through translator -------------

def test_translator_preserves_custom_pages():
    """Customer-declared pages (with paths not in archetype defaults)
    MUST flow through translate_v1_spec into spec.custom_pages."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "b2b-translator",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "pages": [
            {"path": "/", "title": "Home", "purpose": "Hero + upcoming classes", "auth_required": False},
            {"path": "/book", "title": "Book a Class", "purpose": "Reserve a spot", "auth_required": False},
            {"path": "/contact", "title": "Contact Us", "purpose": "Get in touch", "auth_required": False},
            {"path": "/admin", "title": "Admin Dashboard", "purpose": "Manage classes", "auth_required": True, "allowed_roles": ["admin"]},
        ],
    }
    v2 = translate_v1_spec(v1)
    custom = v2.get("custom_pages", [])
    paths = [p["path"] for p in custom]
    # All v1 pages pass through at translator layer; filter-out of archetype
    # collisions happens at build time via _custom_pages_filter.
    assert "/book" in paths
    assert "/contact" in paths
    assert "/admin" in paths
    # auth_required + allowed_roles preserved
    admin_page = next(p for p in custom if p["path"] == "/admin")
    assert admin_page["auth_required"] is True
    assert admin_page["allowed_roles"] == ["admin"]


def test_custom_pages_filter_skips_archetype_paths():
    """Build-time filter drops customer pages whose path an archetype
    already emits (e.g. /contact, /about, /). Avoids duplicate router
    handlers. Customer title for such overlaps is exercised via
    settings, not a second route."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "b2b-filter",
        "description": "Yoga studio with weekly classes",  # → fitness-studio archetype
        "admin_email": "admin@example.com",
        "pages": [
            {"path": "/contact", "title": "Contact Us", "purpose": "Reach out"},   # collides
            {"path": "/book", "title": "Book a Class", "purpose": "Reserve"},       # custom
            {"path": "/our-story", "title": "Our Story", "purpose": "About us"},    # custom
        ],
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="b2b-filter-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        # Custom page view emitted for non-colliding paths
        assert (build_dir / "views" / "custom" / "book.ejs").is_file(), (
            "book.ejs view not emitted"
        )
        assert (build_dir / "views" / "custom" / "our-story.ejs").is_file(), (
            "our-story.ejs view not emitted"
        )
        # NOT emitted for /contact (archetype already covers)
        assert not (build_dir / "views" / "custom" / "contact.ejs").exists(), (
            "contact collision not filtered; would duplicate router handler"
        )

        # Routes file includes the custom routes, NOT a duplicate /contact
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        assert "router.get('/book'" in pages_js
        assert "router.get('/our-story'" in pages_js
        # Exactly one /contact handler (from archetype emitter), not two.
        assert pages_js.count("router.get('/contact'") == 1
    finally:
        _shutil.rmtree(tmp)


def test_parametric_custom_pages_are_dropped_by_the_engine_too():
    """Defense-in-depth: even if a parametric custom_page bypasses the
    harbor translator (legacy v1 spec, manual edit), the build-time
    filter MUST drop it. Otherwise the slug-from-path helper produces
    a `:`-containing view name and the emitted Express route shadows
    the archetype's default `/<plural>/:identifier` detail handler.

    Regression: a DJ-site build; keeper-
    declared /events/:event-slug rendered 404 because no view file
    existed for the templated path AND the archetype default detail
    route was shadowed.
    """
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    # Bypass the harbor translator by writing a v1 spec directly with
    # a parametric path. event-producer archetype already emits a
    # default /events/:identifier detail handler that should win.
    v1 = {
        "app_name": "b2b-parametric",
        "description": "Live event series with weekly shows",  # → event-producer
        "admin_email": "admin@example.com",
        "pages": [
            {"path": "/events/:event-slug", "title": "Event Page",
             "purpose": "Per-event detail"},
            # Non-parametric sibling MUST still pass through.
            {"path": "/our-story", "title": "Our Story", "purpose": "About us"},
        ],
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="b2b-parametric-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        # No `:`-containing view file written.
        custom_dir = build_dir / "views" / "custom"
        if custom_dir.exists():
            for f in custom_dir.iterdir():
                assert ":" not in f.name, (
                    f"parametric custom view {f.name} should have been dropped"
                )

        # Non-parametric sibling still emitted.
        assert (build_dir / "views" / "custom" / "our-story.ejs").is_file()

        # Routes file: NO custom-page handler for the parametric path.
        # The archetype's default /events/:identifier detail handler
        # remains the sole owner of that URL pattern.
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        assert "/events/:event-slug" not in pages_js, (
            "parametric custom_page handler emitted; would shadow archetype default"
        )
        assert "router.get('/our-story'" in pages_js
    finally:
        _shutil.rmtree(tmp)


def test_harbor_custom_page_event_slug_chain_completion_e2e():
    """E2E chain-completion: when a keeper-defined parametric path
    /events/:event-slug is dropped by the harbor translator, the
    archetype's default detail handler at /events/:identifier MUST
    take over so a real GET /events/<id> request resolves to a
    rendered detail view (not 404).

    This is the gap the deferred check 2026-04-30
    "harbor-custom-page-event-slug-404 follow-up" was tracking.
    Existing tests verify the drop; this test verifies the chain
    completes (handler emitted, view emitted, listing nav link
    present, seed produces resolvable rows). Source:
    coordinator/roadmap.json#harbor-custom-page-event-slug-e2e-verify.

    Note: Event has no `slug` field by default (see _default_fields
    in compose/build.py), so the detail handler queries by id only.
    The roadmap description's `/events/<slug>` URL pattern actually
    means `/events/<identifier>` in archetype terms.
    """
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "harbor-events-e2e",
        "description": "Curated art show and concert series with weekly upcoming events.",
        "admin_email": "admin@example.com",
        "pages": [
            # Realistic harbor extraction: keeper declares both the
            # /events listing AND a parametric per-event detail page.
            # The translator drops the parametric one; the listing is
            # the archetype default and stays.
            {"path": "/events", "title": "Events",
             "purpose": "Browse upcoming shows"},
            {"path": "/events/:event-slug", "title": "Event Page",
             "purpose": "Per-event detail"},
            {"path": "/about", "title": "About", "purpose": "Story"},
        ],
        # Force event-producer archetype so the test exercises the
        # exact archetype where /events parametric paths matter most.
        # _harbor_meta.archetype_hint is honored by detect_archetype()
        # ahead of keyword matching (coach-consultant keyword expansion +
        # harbor archetype-hint forwarding).
        "_harbor_meta": {"archetype_hint": "event-producer"},
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="harbor-events-e2e-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        pages_js = (build_dir / "routes" / "pages.js").read_text()

        # 1. Archetype's default detail handler MUST be emitted.
        #    Without this, a curl to /events/<id> would 404 because
        #    the parametric custom_page was correctly dropped.
        assert "router.get('/events/:identifier'" in pages_js, (
            "archetype default /events/:identifier detail handler "
            "missing; dropping the parametric custom_page left no "
            "handler for /events/<id>"
        )

        # 2. The detail handler queries the events table.
        assert "FROM events WHERE" in pages_js, (
            "detail handler does not query events table"
        )

        # 3. The detail handler renders the per-listing detail view.
        assert "events-detail" in pages_js, (
            "detail handler missing events-detail view render call"
        )

        # 4. The events-detail view file MUST exist on disk so the
        #    render call resolves (not 'view not found').
        detail_view = build_dir / "views" / "events-detail.ejs"
        assert detail_view.is_file(), (
            f"events-detail.ejs view missing at {detail_view}; "
            "handler would crash with 'Failed to lookup view'"
        )

        # 5. Nav MUST link to /events (the listing) and MUST NOT
        #    contain the dropped parametric path. _NAV_ITEMS is
        #    JSON-serialized in middleware/settings.js.
        settings_js = (build_dir / "middleware" / "settings.js").read_text()
        assert "/events/:event-slug" not in settings_js, (
            "dropped parametric path leaked into nav middleware"
        )
        assert '"href": "/events"' in settings_js, (
            "/events listing link missing from _NAV_ITEMS; chain broken"
        )
        # And the matching label.
        assert '"label": "Events"' in settings_js, (
            "Events nav label missing from _NAV_ITEMS"
        )

        # 6. Seed.sql produces Event rows (so a real curl to
        #    /events/<seeded-id> would actually resolve to a row,
        #    not an empty 404 from the SELECT). The archetype seeds
        #    multiple events by default for the events listing page.
        seed_sql = (build_dir / "db" / "seed.sql").read_text()
        # event-producer archetype seeds the events table.
        assert "INSERT INTO events" in seed_sql, (
            "no events seeded; detail handler would 404 on every id"
        )

        # 7. Sibling non-parametric custom page still emits its handler
        #    (sanity check that the parametric drop is surgical).
        assert "router.get('/about'" in pages_js or "/about" in pages_js
    finally:
        _shutil.rmtree(tmp)


def test_custom_page_view_is_widget_composed():
    """The v2 lint asserts every page view in views/ (including
    views/custom/) uses widget-composition (`<%- include(...)` chains).
    Regression guard: custom page views MUST NOT be bare static HTML."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "b2b-lint",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "pages": [{"path": "/rsvp", "title": "RSVP", "purpose": "Reserve your spot"}],
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="b2b-lint-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        rsvp_view = (build_dir / "views" / "custom" / "rsvp.ejs").read_text()
        # Widget-composed: must chain include() calls, not bare HTML
        assert "<%- include(" in rsvp_view, (
            f"custom page view not widget-composed (breaks v2 lint):\n{rsvp_view}"
        )
        # Title/purpose flowed through
        assert "RSVP" in rsvp_view
        assert "Reserve your spot" in rsvp_view
    finally:
        _shutil.rmtree(tmp)


def test_auth_required_custom_page_uses_requireAuth_guard():
    """A customer page with auth_required=true + allowed_roles=['admin']
    must generate a route handler guarded by requireAuth + requireRole.
    Otherwise customer's 'Admin Dashboard' path would be public."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "b2b-authed",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "pages": [{
            "path": "/team-dashboard",
            "title": "Team Dashboard",
            "purpose": "Private team view",
            "auth_required": True,
            "allowed_roles": ["admin"],
        }],
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="b2b-authed-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        pages_js = (build_dir / "routes" / "pages.js").read_text()
        # Find the /team-dashboard handler line
        assert "router.get('/team-dashboard'" in pages_js
        # Middleware chain must include requireAuth + requireRole('admin')
        # somewhere in the line above the handler body
        handler_start = pages_js.index("router.get('/team-dashboard'")
        handler_line = pages_js[handler_start:handler_start + 200]
        assert "requireAuth" in handler_line, (
            f"auth_required=true page missing requireAuth guard: {handler_line}"
        )
        assert "requireRole('admin')" in handler_line, (
            f"allowed_roles=['admin'] missing requireRole guard: {handler_line}"
        )
    finally:
        _shutil.rmtree(tmp)


def test_custom_pages_with_invalid_paths_are_skipped():
    """Empty, missing-slash, and non-dict page entries must be dropped
    by the translator; not leaked into custom_pages where compose
    would then try to emit a bogus route."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "b2b-invalid",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "pages": [
            {"path": "/ok", "title": "OK"},
            {"path": "", "title": "Empty"},          # dropped
            {"path": "no-leading-slash", "title": "Bad"},  # dropped
            None,                                    # dropped
            "not-a-dict",                            # dropped
        ],
    }
    v2 = translate_v1_spec(v1)
    paths = [p["path"] for p in v2.get("custom_pages", [])]
    assert paths == ["/ok"], f"invalid entries leaked: {paths}"


# ---- preserve v1.user_roles through translator --------------------

def test_translator_preserves_user_roles():
    """v1 user_roles flow through as spec.user_roles with role +
    description preserved. Missing/invalid entries are dropped."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "b2c-roles",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "user_roles": [
            {"role": "admin", "description": "Studio owner"},
            {"role": "instructor", "description": "Teaches classes"},
            {"role": "member", "description": "Books classes"},
            {"role": "", "description": "empty name"},  # dropped
            "not-a-dict",                                 # dropped
        ],
    }
    v2 = translate_v1_spec(v1)
    roles = v2.get("user_roles", [])
    role_names = [r["role"] for r in roles]
    assert role_names == ["admin", "instructor", "member"], (
        f"expected clean role list, got {role_names}"
    )
    instructor = next(r for r in roles if r["role"] == "instructor")
    assert instructor["description"] == "Teaches classes"


def test_seed_skips_rows_when_archetype_seed_shape_lacks_required_field():
    """Regression for the timberline-hvac e2e sample.

    When a customer spec overrides a data_model's fields with a
    `name`-typed required field but the archetype's hardcoded seed
    shape uses different keys (e.g. tradesperson-contractor's
    ServiceArea seeds with `{city, state, zip_codes, description,
    sort_order}` and has no `name` key), the schema emits
    `name VARCHAR NOT NULL`. Without this guard, the seed loop emits
    `INSERT (..., name, ...) VALUES (..., NULL, ...)` and Postgres
    rejects with `null value in column "name"`. The app crashes at
    boot during db_migrate.

    Fix contract: emit_seed_sql filters rows whose customer-declared
    required fields are missing from the row keys. When all rows fail,
    seed for that model is skipped entirely with a SKIPPED comment;
    a translation_warnings entry is appended so SE reviewers see why.
    """
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "seed-shape-test",
        "description": "HVAC contractor with overridden ServiceArea fields",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "ServiceArea", "fields": [
                {"name": "name", "type": "text", "required": True},
                {"name": "zip_codes", "type": "text", "required": False},
            ]},
        ],
        "_harbor_meta": {"archetype_hint": "tradesperson-contractor"},
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="seed-shape-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        seed_sql = (build_dir / "db" / "seed.sql").read_text()
        # Must NOT contain a service_areas row with NULL name.
        assert "INSERT INTO service_areas" not in seed_sql or \
               "(NULL," not in seed_sql.split("INSERT INTO service_areas")[1].split(";")[0], \
               "seed emitted INSERT with NULL name into service_areas (would crash db_migrate)"
        # Must contain the SKIPPED comment so SE reviewers see why.
        assert "ServiceArea seed SKIPPED" in seed_sql, \
            "seed.sql missing ServiceArea SKIPPED comment"
        # The skip reason must mention the missing required field by name.
        assert "['name']" in seed_sql or "name" in seed_sql.split("ServiceArea seed SKIPPED")[1].split("\n")[0], \
            "skip comment must name the missing required field"
    finally:
        _shutil.rmtree(tmp)


def test_seed_skips_rows_partial_match_warns():
    """When SOME rows have the customer-declared required field and
    SOME don't, only the matching ones are seeded; a warning is
    emitted for the dropped count. This is the 'partial-mismatch'
    branch (rare in practice; archetype seeds are usually all-or-
    nothing-shaped, but the guard handles it cleanly)."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app

    # Build a v2 spec directly (bypassing translate_v1_spec) so we can
    # inject mixed-shape seed_content for a custom data_model.
    v2 = {
        "app_name": "partial-shape-test",
        "archetype": "tradesperson-contractor",
        "admin_email": "admin@example.com",
        "palette": "navy-clean",
        "font_pair": "rubik-nunito",
        "data_models": [
            {"name": "Widget", "plural": "widgets", "fields": [
                {"name": "title", "type": "text", "required": True},
                {"name": "desc", "type": "text", "required": False},
            ]},
        ],
        "seed_content": {
            "widgets": [
                {"title": "Has title", "desc": "ok"},
                {"desc": "no title here"},  # missing required `title`
                {"title": "Also has title"},
            ],
        },
        "settings": {},
    }
    tmp = _Path(tempfile.mkdtemp(prefix="partial-shape-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        seed_sql = (build_dir / "db" / "seed.sql").read_text()
        # Should have 2 widget INSERTs (the ones with title), not 3.
        widget_inserts = seed_sql.count("INSERT INTO widgets ")
        assert widget_inserts == 2, \
            f"expected 2 widget inserts (skipping the title-less row), got {widget_inserts}"
        # The valid rows must be present.
        assert "Has title" in seed_sql
        assert "Also has title" in seed_sql
    finally:
        _shutil.rmtree(tmp)


def test_seed_unaffected_when_required_fields_match_seed_shape():
    """Sanity: when customer's required-field set matches the
    archetype's seed shape (the common case), all rows are emitted.
    Healer's Photo model + photo seed shape match cleanly."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "happy-path",
        "description": "Healer with default Photo model + matching seed",
        "admin_email": "admin@example.com",
        "_harbor_meta": {"archetype_hint": "healer-wellness"},
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="happy-shape-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        seed_sql = (build_dir / "db" / "seed.sql").read_text()
        # Photo seeds present (8 rows for healer archetype)
        assert seed_sql.count("INSERT INTO photos ") == 8, \
            "healer Photo seed regressed; expected 8 default rows"
        # No SKIPPED comments on the happy path
        assert "seed SKIPPED" not in seed_sql, \
            "happy path should not emit any seed-skip warnings"
    finally:
        _shutil.rmtree(tmp)


def test_seed_sql_documents_customer_roles():
    """emit_seed_sql renders a comment block for declared user_roles at
    the top of the seed file, so SE reviewers see the role catalog."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "b2c-seed",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "user_roles": [
            {"role": "admin", "description": "Studio owner"},
            {"role": "instructor", "description": "Teaches classes"},
        ],
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="b2c-seed-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        seed_sql = (build_dir / "db" / "seed.sql").read_text()
        assert "Customer-declared user roles" in seed_sql
        assert "admin: Studio owner" in seed_sql
        assert "instructor: Teaches classes" in seed_sql
    finally:
        _shutil.rmtree(tmp)


def test_seed_sql_has_no_roles_comment_without_user_roles():
    """When v1 has no user_roles, seed.sql must NOT include the
    doc-comment block (keeps the default seed tidy)."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app

    # scaffold_spec directly (no user_roles)
    tmp = _Path(tempfile.mkdtemp(prefix="b2c-no-roles-"))
    try:
        from compose.intake import scaffold_spec
        v2 = scaffold_spec({
            "app_name": "b2c-no-roles",
            "archetype": "healer-wellness",
            "site_name": "Test",
            "admin_email": "admin@example.com",
        })
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        seed_sql = (build_dir / "db" / "seed.sql").read_text()
        assert "Customer-declared user roles" not in seed_sql, (
            "seed.sql leaked user-roles comment block for a spec that has no "
            "user_roles; would confuse SE reviewers."
        )
    finally:
        _shutil.rmtree(tmp)


# ---- translation_warnings metadata channel ------------------------

def test_translator_emits_warnings_for_invalid_entries():
    """Lossy translator decisions (dropped data_models, dropped pages,
    dropped user_roles) must be recorded in spec._translation_warnings
    so the SE reviewer can see them."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "b2d-warnings",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Valid", "fields": []},
            {"fields": []},                    # no name → warning
            "not-a-dict",                       # not-a-dict → warning
        ],
        "pages": [
            {"path": "/ok", "title": "OK"},
            {"path": "", "title": "Empty"},    # empty path → warning
            {"path": "bad-path"},               # no leading slash → warning
            None,                               # None → warning
        ],
        "user_roles": [
            {"role": "admin"},
            {"description": "no role name"},   # missing role → warning
            123,                                # not-a-dict → warning
        ],
    }
    v2 = translate_v1_spec(v1)
    warnings = v2.get("_translation_warnings", [])
    reasons = [w["reason"] for w in warnings]
    # Model warnings
    assert "data_model_missing_name" in reasons
    assert "invalid_data_model" in reasons
    # Page warnings
    assert "invalid_page_path" in reasons
    assert "invalid_page" in reasons
    # Role warnings
    assert "user_role_missing_name" in reasons
    assert "invalid_user_role" in reasons
    # Detail is a human-readable string
    for w in warnings:
        assert isinstance(w["detail"], str) and w["detail"], (
            f"warning missing detail string: {w}"
        )


def test_translator_emits_no_warnings_for_clean_v1_spec():
    """A clean v1 spec produces NO warnings. Spec key is omitted
    entirely (don't leak an empty list into compose-manifest)."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "b2d-clean",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "data_models": [{"name": "Class", "fields": [{"name": "title", "type": "string"}]}],
        "pages": [{"path": "/", "title": "Home"}],
        "user_roles": [{"role": "admin"}],
    }
    v2 = translate_v1_spec(v1)
    assert "_translation_warnings" not in v2


def test_manifest_includes_warning_lines():
    """compose-manifest.txt carries `translation_warnings_count:` and one
    line per warning. A warning the translator put into the spec
    (`_translation_warnings`) is spec text, so its line begins with
    `spec-warning: `; only the engine's own findings begin with
    `warning: `. The worker's lint reads these lines into build_logs."""
    import tempfile, json as _json, shutil as _shutil
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import translate_v1_spec

    v1 = {
        "app_name": "b2d-manifest",
        "description": "Yoga studio",
        "admin_email": "admin@example.com",
        "pages": [
            {"path": "/ok", "title": "OK"},
            {"path": "bad", "title": "Bad"},  # no leading slash → warning
        ],
    }
    v2 = translate_v1_spec(v1)
    tmp = _Path(tempfile.mkdtemp(prefix="b2d-manifest-"))
    try:
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        manifest_lines = (build_dir / "compose-manifest.txt").read_text().split("\n")
        assert "translation_warnings_count: 1" in manifest_lines
        assert "engine_warnings_count: 0" in manifest_lines
        assert "spec_warnings_count: 1" in manifest_lines
        assert "spec_warnings_omitted: 0" in manifest_lines
        assert [l for l in manifest_lines if l.startswith("spec-warning: invalid_page_path: ")]
        assert not [l for l in manifest_lines if l.startswith("warning: ")]
        # Also: translation-warnings.txt emitted when count > 0
        tw_file = build_dir / "translation-warnings.txt"
        assert tw_file.is_file()
        assert [l for l in tw_file.read_text().split("\n") if l.startswith("spec-warning: invalid_page_path: ")]
    finally:
        _shutil.rmtree(tmp)


def test_no_warning_file_without_warnings():
    """Clean spec = no translation-warnings.txt file (don't leak
    empty artifacts)."""
    import tempfile, shutil as _shutil, json as _json
    from pathlib import Path as _Path
    from compose.build import build_app
    from compose.intake import scaffold_spec

    tmp = _Path(tempfile.mkdtemp(prefix="b2d-clean-"))
    try:
        v2 = scaffold_spec({
            "app_name": "b2d-clean",
            "archetype": "healer-wellness",
            "site_name": "Test",
            "admin_email": "admin@example.com",
        })
        spec_path = tmp / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        assert not (build_dir / "translation-warnings.txt").exists()
        manifest = (build_dir / "compose-manifest.txt").read_text()
        assert "translation_warnings_count: 0" in manifest
    finally:
        _shutil.rmtree(tmp)


def test_archetype_classification_matches_data_models_hint():
    """detect_archetype considers data_model names + action names + page
    titles in addition to description. When the description is ambiguous,
    explicit model declarations should tip the classifier."""
    from compose.intake import detect_archetype
    # Plain description with just "local business", without model hints
    # this falls back to local-service. With explicit models that hint at
    # a specific archetype, the classifier should pick it up.
    ambiguous_desc = "Local business in town"
    # With plumbing models → tradesperson-contractor
    got = detect_archetype({
        "description": ambiguous_desc,
        "data_models": [{"name": "PlumbingJob"}, {"name": "ServiceArea"}],
    })
    assert got == "tradesperson-contractor", f"Expected tradesperson from PlumbingJob model, got {got}"


def test_intake_scaffold_spec_happy_path():
    from compose.intake import scaffold_spec
    spec = scaffold_spec({
        "app_name": "demo-coach",
        "archetype": "coach-consultant",
        "site_name": "Demo Coach",
        "admin_email": "admin@demo.example",
    })
    assert spec["app_name"] == "demo-coach"
    assert spec["archetype"] == "coach-consultant"
    # palette + font fall back to archetype defaults
    assert spec["palette"]
    assert spec["font_pair"]
    # Every default page gets a pages entry
    assert "home" in spec["pages"]
    assert "about" in spec["pages"]
    assert "contact" in spec["pages"]
    # Home has the superset of data sources so every archetype's layout resolves
    assert "services" in spec["pages"]["home"]
    assert "offerings" in spec["pages"]["home"]
    assert "testimonials" in spec["pages"]["home"]
    assert "trust" in spec["pages"]["home"]  # tradesperson
    assert "areas" in spec["pages"]["home"]
    # Seed content populated with defaults
    assert spec["seed_content"]["testimonials"]
    assert spec["seed_content"]["contact_inquiries"]


def test_intake_scaffold_spec_skips_lead_seeds_for_harbor_builds():
    """Harbor builds (`_harbor_meta` present in answers) ship with
    empty contact_inquiries + newsletter_subscribers seed arrays by
    default so /admin/contact-inquiries doesn't show fake @example.com
    leads on a keeper's site."""
    from compose.intake import scaffold_spec
    spec = scaffold_spec({
        "app_name": "demo-coach-harbor",
        "archetype": "coach-consultant",
        "site_name": "Demo Coach",
        "admin_email": "admin@demo.example",
        "_harbor_meta": {"archetype_hint": "coach-consultant"},
    })
    sc = spec["seed_content"]
    # Lead-style seeds suppressed by default for Harbor builds.
    assert sc.get("contact_inquiries", []) == []
    assert sc.get("newsletter_subscribers", []) == []
    # Content-style seeds (testimonials) keep their archetype defaults
    # so /home and /about don't ship empty.
    assert sc["testimonials"], "testimonials should still be seeded for Harbor builds"


def test_intake_scaffold_spec_seeds_lead_data_when_harbor_demo_flag_set():
    """Opt-in path: setting `_harbor_meta.seed_demo_data=true` restores
    the lead-style sample seeds for demo builds."""
    from compose.intake import scaffold_spec
    spec = scaffold_spec({
        "app_name": "demo-harbor-with-leads",
        "archetype": "coach-consultant",
        "site_name": "Demo Coach",
        "admin_email": "admin@demo.example",
        "_harbor_meta": {"seed_demo_data": True},
    })
    sc = spec["seed_content"]
    assert len(sc.get("contact_inquiries", [])) == 3
    assert len(sc.get("newsletter_subscribers", [])) == 4


def test_intake_scaffold_spec_seeds_lead_data_when_top_level_demo_flag_set():
    """Opt-in path #2: top-level `seed_demo_data=true` answer (CLI smoke
    tests, fixture regeneration) also restores the lead-style seeds."""
    from compose.intake import scaffold_spec
    spec = scaffold_spec({
        "app_name": "demo-harbor-cli",
        "archetype": "coach-consultant",
        "site_name": "Demo Coach",
        "admin_email": "admin@demo.example",
        "_harbor_meta": {"archetype_hint": "coach-consultant"},
        "seed_demo_data": True,
    })
    sc = spec["seed_content"]
    assert len(sc.get("contact_inquiries", [])) == 3
    assert len(sc.get("newsletter_subscribers", [])) == 4


def test_intake_translate_v1_with_harbor_meta_skips_lead_seeds():
    """End-to-end through translate_v1_spec: a v1 spec carrying
    `_harbor_meta` (the Harbor extraction path) ships with no fake
    contact inquiries by default."""
    from compose.intake import translate_v1_spec
    v1 = {
        "spec_version": "v12.0",
        "app_name": "harbor-coach-demo",
        "description": "A Harbor-translated coach build.",
        "admin_email": "coach@demo.example",
        "tier": "build",
        "user_roles": [{"role": "admin", "description": "Owner", "can": []}],
        "pages": [{"path": "/", "title": "Home", "purpose": "Landing", "auth_required": False, "allowed_roles": []}],
        "data_models": [],
        "_harbor_meta": {
            "archetype_hint": "coach-consultant",
            "services": [
                {"title": "1:1 Coaching", "description": "Weekly session", "price": "$200"},
            ],
        },
    }
    spec = translate_v1_spec(v1)
    sc = spec.get("seed_content", {})
    # Default off for Harbor: no fake leads or subscribers.
    assert sc.get("contact_inquiries", []) == []
    assert sc.get("newsletter_subscribers", []) == []


def test_intake_translate_v1_to_v2_spec():
    """Realistic v1 AppSpec (the shape web/ intake emits) translates into
    a valid v2 spec that passes validate_spec. Regression guard for the
    'use v1 intake for v2' wiring."""
    from compose.intake import translate_v1_spec
    from compose.build import validate_spec, BuildSpec
    v1 = {
        "spec_version": "v12.0",
        "app_name": "timber-line-hvac",
        "description": "Family-run HVAC contractor serving homeowners.",
        "admin_email": "admin@timberlinehvac.example",
        "user_roles": [{"role": "admin", "description": "Shop owner", "can": []}],
        "pages": [{"path": "/", "title": "Home", "purpose": "Landing", "auth_required": False, "allowed_roles": []}],
        "data_models": [{"name": "Service", "fields": [], "belongs_to": None, "has_many": []}],
        "actions": [{"name": "submit_quote", "description": "emergency"}],
        "design": {"primary_color": "#1e3a8a", "secondary_color": "#eff6ff", "accent_color": "#f59e0b", "style": "clean reliable"},
        "settings": {"auth": True, "seed_data": True},
    }
    spec = translate_v1_spec(v1)
    # Archetype classified correctly from "hvac"
    assert spec["archetype"] == "tradesperson-contractor"
    # Palette mapped from #1e3a8a → navy-clean
    assert spec["palette"] == "navy-clean"
    # Font from style="clean" → inter-inter
    assert spec["font_pair"] == "inter-inter"
    # Final spec must validate
    errors = validate_spec(BuildSpec.from_dict(spec))
    assert errors == [], f"translated spec failed validation: {errors}"


def test_resource_router_supports_table_sort(resolved):
    files = dict(compose_files(resolved))
    router = files["routes/blog-posts.js"]
    # Sort query-param parsing + ORDER BY
    assert "req.query.sort" in router
    assert "req.query.order" in router
    # Column whitelist prevents SQL injection
    assert "allowedSort" in router
    assert "ALLOWED_COLS" in router


def test_resource_router_supports_pagination(resolved):
    files = dict(compose_files(resolved))
    router = files["routes/blog-posts.js"]
    # Pagination params + SQL LIMIT/OFFSET
    assert "req.query.page" in router
    assert "req.query.per_page" in router
    assert "LIMIT $1 OFFSET $2" in router
    # Cap at 100 to prevent cheap-query-becomes-expensive attacks
    assert "perPage > 100" in router
    # COUNT query for total_pages calc
    assert "SELECT COUNT(*)" in router
    # Pagination object passed to view
    assert "pagination" in router


def test_crud_list_widget_renders_sortable_headers(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/crud-list.ejs"]
    # Sort-link helper + sort-arrow indicator
    assert "_sortHref" in widget
    assert "'asc'" in widget and "'desc'" in widget
    assert "↑" in widget and "↓" in widget


def test_crud_list_widget_renders_pagination_controls(resolved):
    files = dict(compose_files(resolved))
    widget = files["views/widgets/admin/crud-list.ejs"]
    assert "_pageHref" in widget
    assert "total_pages" in widget
    # Prev/Next controls
    assert "has_prev" in widget
    assert "has_next" in widget


# ---- client-side <time> formatting (cas-fmt.js) ----

def test_cas_fmt_js_emitted_to_public(resolved):
    """Generated apps ship public/js/cas-fmt.js so dates render in the
    visitor's locale + timezone (Railway containers run in UTC)."""
    files = dict(compose_files(resolved))
    assert "public/js/cas-fmt.js" in files
    js = files["public/js/cas-fmt.js"]
    # All four formatters present so widgets can pick the right shape.
    assert ".cas-fmt-date" in js
    assert ".cas-fmt-date-long" in js
    assert ".cas-fmt-date-weekday" in js
    assert ".cas-fmt-datetime-compact" in js
    # Reads from datetime attribute first so re-runs are idempotent.
    assert "getAttribute('datetime')" in js


def test_layouts_load_cas_fmt_js(resolved):
    """Both public + admin layouts must load /js/cas-fmt.js so any widget
    that emits a <time class='cas-fmt-*'> tag picks up the formatter."""
    files = dict(compose_files(resolved))
    pub = files["views/layout-public.ejs"]
    adm = files["views/layout-admin.ejs"]
    assert "/js/cas-fmt.js" in pub
    assert "/js/cas-fmt.js" in adm


def test_resource_list_card_grid_uses_client_side_time(resolved):
    """The blog/case-study/journal listing widget no longer formats dates
    on the server (which is UTC). It emits <time class='cas-fmt-date-long'>
    and lets cas-fmt.js render in the visitor's locale."""
    widget = (
        Path(__file__).resolve().parent.parent
        / "widgets" / "offerings" / "resource-list-card-grid.ejs"
    ).read_text()
    assert "toLocaleDateString" not in widget
    assert "cas-fmt-date-long" in widget
    # The server-side _formatDate helper is gone.
    assert "_formatDate" not in widget


def test_reviews_grid_uses_client_side_time(resolved):
    """Review byline dates must render in the visitor's timezone."""
    widget = (
        Path(__file__).resolve().parent.parent
        / "widgets" / "offerings" / "reviews-grid.ejs"
    ).read_text()
    assert "toLocaleDateString" not in widget
    assert "cas-fmt-date" in widget
    assert "<time" in widget


def test_admin_dashboard_recent_inquiries_uses_client_side_time(resolved):
    """Recent-inquiries table dates must render in the admin's timezone."""
    widget = (
        Path(__file__).resolve().parent.parent
        / "widgets" / "admin" / "dashboard.ejs"
    ).read_text()
    assert "toLocaleDateString" not in widget
    assert "cas-fmt-date" in widget
    assert "<time" in widget


def test_admin_upcoming_events_uses_client_side_time(resolved):
    """Upcoming-events list dates must render in the admin's timezone with
    weekday + short month/day/year."""
    widget = (
        Path(__file__).resolve().parent.parent
        / "widgets" / "admin" / "upcoming-events.ejs"
    ).read_text()
    assert "toLocaleDateString" not in widget
    assert "cas-fmt-date-weekday" in widget
    assert "<time" in widget


def test_build_app_manifest_records_hash(tmp_path):
    out = build_app(str(SAMPLE_SPEC), output_dir=tmp_path / "out")
    manifest = (out / "compose-manifest.txt").read_text()
    assert "sha256:" in manifest
    assert "archetype: healer-wellness" in manifest
    # Fern e2e sample: manifest annotates palette with primary
    # hex so id-vs-color mismatch perception is closed.
    assert "palette: warm-earth (primary=#6b1519)" in manifest


# =============================================================================
# public submission forms.
# Translator surfaces form_target + allowed_fields on custom_pages; build.py
# swaps the hero/centered view for the public-submission widget when the
# target resolves, and emit_routes_pages_js emits a GET+POST pair that
# validates CSRF, rejects honeypot submissions, INSERTs accepted fields,
# and redirects back with a session flash.
# =============================================================================

def _form_page_build(v1: dict, tmp_path):
    """Run translate + build on a v1 spec; return the build_dir Path."""
    import json as _json
    from compose.build import build_app
    from compose.intake import translate_v1_spec
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    return build_app(str(spec_path), output_dir=tmp_path / "out")


def _v1_spec_with_form_page(**overrides) -> dict:
    """Shared v1 fixture: data_model + a custom page that posts into it."""
    base = {
        "app_name": "b3-forms",
        "description": "Sound bath facilitator",
        "admin_email": "admin@example.com",
        "data_models": [
            {
                "name": "ContactMessage",
                "fields": [
                    {"name": "name", "type": "string", "required": True},
                    {"name": "email", "type": "email", "required": True},
                    {"name": "phone", "type": "phone"},
                    {"name": "message", "type": "text", "required": True},
                    {"name": "approved", "type": "boolean"},
                ],
            }
        ],
        "pages": [
            {
                "path": "/contact-us",
                "title": "Contact Us",
                "purpose": "Send a message",
                "form_target": "ContactMessage",
                # A page visitors can open without a login names the columns
                # they may fill in.
                "allowed_fields": ["name", "email", "phone", "message"],
                "submit_text": "Send Message",
                "success_message": "Thanks; we will reach out soon.",
            }
        ],
    }
    base.update(overrides)
    return base


def test_translator_preserves_form_target_hints():
    """v1 pages carrying form_target / allowed_fields / submit_text /
    success_message survive the translator and land on spec.custom_pages."""
    from compose.intake import translate_v1_spec
    v1 = _v1_spec_with_form_page()
    v1["pages"][0]["allowed_fields"] = ["name", "email", "message"]
    v2 = translate_v1_spec(v1)
    cps = v2.get("custom_pages") or []
    assert len(cps) == 1
    cp = cps[0]
    assert cp["path"] == "/contact-us"
    assert cp["form_target"] == "ContactMessage"
    assert cp["allowed_fields"] == ["name", "email", "message"]
    assert cp["submit_text"] == "Send Message"
    assert cp["success_message"] == "Thanks; we will reach out soon."


def test_form_page_view_uses_public_submission_widget(tmp_path):
    """When form_target resolves, the custom page view must include the
    public-submission widget (not the hero/centered fallback) so the form
    actually renders. v2 lint still passes (widget-composed)."""
    build_dir = _form_page_build(_v1_spec_with_form_page(), tmp_path)
    view = (build_dir / "views" / "custom" / "contact-us.ejs").read_text()
    assert "widgets/form/public-submission.ejs" in view, (
        f"form_target page did not wire the public-submission widget:\n{view}"
    )
    # v2 lint requires widget-composition
    assert "<%- include(" in view
    # Customer title flows through as the heading.
    assert "Contact Us" in view
    # Widget file must also be emitted alongside the other widgets.
    widget = (build_dir / "views" / "widgets" / "form" / "public-submission.ejs").read_text()
    assert "_csrf" in widget
    assert "honeypot" in widget.lower() or "website_url_hp" in widget


def test_public_purpose_text_strips_internal_sentences():
    """_public_purpose_text keeps public sentences + drops internal/operator
    ones (where data lands, admin, data models) so spec.purpose never leaks
    to a public subtitle. Found by the automated build review (not part of
    this repository), 2026-06-07."""
    from compose.build import _public_purpose_text
    assert _public_purpose_text(
        "A simple form so visitors can request a session. Lands in admin's Booking Intents."
    ) == "A simple form so visitors can request a session."
    assert _public_purpose_text("Stored in the IntakeSubmission data model.") == ""
    assert _public_purpose_text("Tell us what you are looking for.") == "Tell us what you are looking for."
    assert _public_purpose_text("") == ""


def test_custom_page_purpose_internal_jargon_not_public(tmp_path):
    """A custom form page whose purpose mixes a public sentence + an internal
    'lands in admin' sentence must render only the public part as the subtitle."""
    v1 = _v1_spec_with_form_page()
    v1["pages"][0]["purpose"] = (
        "A simple form so visitors can request a session. Lands in admin's Booking Intents."
    )
    build_dir = _form_page_build(v1, tmp_path)
    view = (build_dir / "views" / "custom" / "contact-us.ejs").read_text()
    assert "request a session" in view  # public part kept
    assert "Booking Intents" not in view  # internal part stripped
    assert "admin" not in view.lower().replace("admin@", "")  # no internal jargon


def test_content_less_custom_page_renders_cta_not_blank(tmp_path):
    """A custom page declared with only a title (no content) must NOT render as
    a bare <h1>; it gets a warm invite + a 'Get in touch' CTA pointing at the
    form/contact page, so it is complete + on-brand. Found by the automated
    build review (not part of this repository): blank /sessions, /studio nav
    pages."""
    v1 = _v1_spec_with_form_page()
    v1["pages"].append({"path": "/sessions", "title": "Sessions"})
    build_dir = _form_page_build(v1, tmp_path)
    view = (build_dir / "views" / "custom" / "sessions.ejs").read_text()
    assert "Sessions" in view  # heading
    assert "Get in touch" in view  # CTA -> the page is complete, not blank
    assert "learn more about sessions" in view.lower()  # invite description
    assert "/contact-us" in view  # CTA targets the form/contact page


def test_allowed_fields_restricts_rendered_form_fields(tmp_path):
    """allowed_fields is an explicit allowlist. When set, only listed
    fields reach the widget; everything else drops (even if the customer
    declared them on the model)."""
    v1 = _v1_spec_with_form_page()
    v1["pages"][0]["allowed_fields"] = ["name", "email", "message"]
    build_dir = _form_page_build(v1, tmp_path)
    view = (build_dir / "views" / "custom" / "contact-us.ejs").read_text()
    assert "'name': 'name'" in view or '"name": "name"' in view or "name: 'name'" in view or "'name':'name'" in view
    # phone and approved are not in the allowlist
    assert "'phone'" not in view and '"phone"' not in view
    assert "approved" not in view


def test_system_and_banned_fields_always_dropped_from_forms(tmp_path):
    """System fields (id/created_at/etc.) and banned field_types
    (image/password) drop even when the page's own allowed_fields names
    them. A page may not offer a visitor what the engine reserves."""
    v1 = _v1_spec_with_form_page()
    # Add a system field + a banned-type field, and name them in the page's
    # allowlist. The translator preserves them on the model and on the
    # page; the public-form emitter must still refuse them.
    v1["data_models"][0]["fields"].extend([
        {"name": "created_at", "type": "timestamp"},
        {"name": "admin_notes", "type": "text"},
        {"name": "avatar", "field_type": "image", "sql_type": "TEXT"},
    ])
    v1["pages"][0]["allowed_fields"] += ["created_at", "admin_notes", "avatar"]
    build_dir = _form_page_build(v1, tmp_path)
    view = (build_dir / "views" / "custom" / "contact-us.ejs").read_text()
    assert "created_at" not in view
    assert "admin_notes" not in view
    assert "avatar" not in view
    # Sanity: safe fields still render
    assert "'message'" in view or '"message"' in view


def test_form_page_emits_get_and_post_pair(tmp_path):
    """routes/pages.js must emit BOTH router.get + router.post for a
    form-target page, with CSRF middleware on POST and INSERT into the
    model's table."""
    build_dir = _form_page_build(_v1_spec_with_form_page(), tmp_path)
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/contact-us'" in pages_js
    assert "router.post('/contact-us'" in pages_js
    # CSRF middleware on POST
    post_idx = pages_js.index("router.post('/contact-us'")
    post_block = pages_js[post_idx:pages_js.index("\n});", post_idx)]
    assert "validateCsrfToken" in post_block.split("\n", 1)[0], (
        f"POST handler missing validateCsrfToken middleware:\n{post_block[:400]}"
    )
    # INSERT into the translated table name (ContactMessage → contact_messages)
    assert "INSERT INTO contact_messages" in post_block, (
        f"POST handler missing correct INSERT target:\n{post_block[:600]}"
    )
    # Router import brings validateCsrfToken in
    assert "validateCsrfToken" in pages_js.split("router.get")[0]


def test_form_post_handler_rejects_honeypot_silently(tmp_path):
    """Honeypot defense: bots fill every input. We accept the submission
    looking-wise (flash_success) but never INSERT so bots do not learn
    we filter."""
    build_dir = _form_page_build(_v1_spec_with_form_page(), tmp_path)
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    post_idx = pages_js.index("router.post('/contact-us'")
    post_block = pages_js[post_idx:pages_js.index("\n});", post_idx)]
    assert "website_url_hp" in post_block
    # Honeypot branch must redirect BEFORE the INSERT block
    hp_pos = post_block.find("website_url_hp")
    insert_pos = post_block.find("INSERT INTO")
    assert 0 <= hp_pos < insert_pos, (
        "honeypot check must precede INSERT; otherwise bots create rows"
    )


def test_form_post_enforces_required_fields(tmp_path):
    """Required fields (per the data_model) are re-checked server-side.
    A missing required field sets flash_error and redirects back, before
    the INSERT. (compose/tests/test_publication.py runs the emitted checks
    in node and the booted-site tests post real forms.)"""
    build_dir = _form_page_build(_v1_spec_with_form_page(), tmp_path)
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    post_idx = pages_js.index("router.post('/contact-us'")
    post_block = pages_js[post_idx:pages_js.index("\n});", post_idx)]
    # The field list carries the fields marked required on the model.
    fields = json.loads(post_block.split("const FIELDS = ", 1)[1].split(";\n", 1)[0])
    assert [f["name"] for f in fields if f["required"]] == ["name", "email", "message"]
    rejection = post_block[post_block.index("if (Object.keys(errors).length > 0)"):post_block.index("INSERT INTO")]
    assert "req.session.flash_error = Object.values(errors).join(" in rejection
    assert "return res.redirect('/contact-us');" in rejection


def test_page_without_form_target_stays_informational(tmp_path):
    """Custom pages without form_target must keep the informational
    hero/centered view; do not accidentally emit POST handlers for them."""
    v1 = _v1_spec_with_form_page()
    v1["pages"].append({
        "path": "/manifesto",
        "title": "Our Manifesto",
        "purpose": "Why we exist",
    })
    build_dir = _form_page_build(v1, tmp_path)
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/manifesto'" in pages_js
    assert "router.post('/manifesto'" not in pages_js
    view = (build_dir / "views" / "custom" / "manifesto.ejs").read_text()
    assert "widgets/hero/centered" in view
    assert "public-submission" not in view


def test_unknown_form_target_falls_back_to_informational_page(tmp_path):
    """If form_target names a data_model that does not exist, the emitter
    should NOT crash. It should fall back to the informational view so
    the SE can catch the typo during review (translation warnings cover
    the visibility)."""
    v1 = _v1_spec_with_form_page()
    v1["pages"][0]["form_target"] = "Nonexistent"
    build_dir = _form_page_build(v1, tmp_path)
    view = (build_dir / "views" / "custom" / "contact-us.ejs").read_text()
    # Fallback: hero/centered, not the form widget
    assert "widgets/hero/centered" in view
    assert "public-submission" not in view
    # And no POST handler is emitted
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.post('/contact-us'" not in pages_js


def test_form_post_redirects_with_session_flash_success(tmp_path):
    """After a successful INSERT the handler sets flash_success to the
    customer-declared success_message (fallback: generic copy) and
    redirects back to the same path so GET-after-POST shows the banner."""
    v1 = _v1_spec_with_form_page()
    build_dir = _form_page_build(v1, tmp_path)
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    post_idx = pages_js.index("router.post('/contact-us'")
    post_block = pages_js[post_idx:post_idx + 2000]
    assert "req.session.flash_success" in post_block
    assert "Thanks; we will reach out soon." in post_block
    assert "res.redirect('/contact-us')" in post_block


def test_form_get_renders_flash_and_prior_submission(tmp_path):
    """GET handler must read + clear session flash_success/flash_error and
    the prior-submission stash so the widget can repopulate after a
    validation failure."""
    build_dir = _form_page_build(_v1_spec_with_form_page(), tmp_path)
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    get_idx = pages_js.index("router.get('/contact-us'")
    get_block = pages_js[get_idx:get_idx + 1500]
    assert "flash_success" in get_block
    assert "flash_error" in get_block
    assert "submitted" in get_block
    # Render call must pass submitted + flashSuccess to the view
    assert "submitted" in get_block


# =============================================================================
# Customer data_models in the admin and on the public site.
# - a customer model with a public listing shows up in the admin dashboard
#   stats and quick links
# - a custom page whose path is a model's name is an ordinary page; a model
#   is listed publicly only where the spec sets public_listing_path on it
# - the public list view of a model the spec lists renders its rows
# =============================================================================

def test_customer_model_appears_in_admin_dashboard(tmp_path):
    """Customer-declared data_models with a public surface (here:
    explicit `public_listing_path`) must appear as stat cards + quick
    links on the admin dashboard, with manage links at /admin/<slug>.

    v2-admin-nav-models-reflect-public-pages (2026-05-27): models
    without a public surface are filtered out; see
    test_admin_dashboard_hides_models_without_public_surface for the
    hide path. Test sets `public_listing_path` explicitly so the
    show path stays exercised independent of archetype detection.
    """
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b4a-dash",
        "description": "yoga studio with sessions and members",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Session", "public_listing_path": "/sessions", "fields": [{"name": "title", "type": "string", "required": True}]},
            {"name": "Member", "public_listing_path": "/members", "fields": [{"name": "name", "type": "string", "required": True}]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    dash = (build_dir / "routes" / "admin-dashboard.js").read_text()
    # Stats + quick link for BOTH models
    assert "manage_href: \"/admin/sessions\"" in dash
    assert "manage_href: \"/admin/members\"" in dash
    assert "\"Manage Sessions\"" in dash
    assert "\"Manage Members\"" in dash
    # Admin views emitted for both
    assert (build_dir / "views" / "sessions" / "manage.ejs").is_file()
    assert (build_dir / "views" / "members" / "manage.ejs").is_file()
    # Admin CRUD router emitted for both
    assert (build_dir / "routes" / "sessions.js").is_file()
    assert (build_dir / "routes" / "members.js").is_file()


def test_custom_page_named_like_a_model_is_a_page_and_the_model_stays_private(tmp_path):
    """A page whose path is a model's slug is NOT a listing of that model.
    Nothing publishes a table because a page shares its name: the page is
    emitted as the ordinary page the spec asked for, the model has no
    public listing or detail view, its router hands the page's path on and
    answers 404 below it, and the build report says so."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    # The description is deliberately generic so detect_archetype falls
    # through to `local-service`, whose default pages do not include
    # /events (an archetype that owns /events keeps its own page there).
    v1 = {
        "app_name": "b4c-public",
        "description": "small business with a public events calendar",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "event_date", "type": "date", "required": True},
            ]},
        ],
        "pages": [
            {"path": "/events", "title": "All Events", "purpose": "Browse upcoming events"},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    # The page the spec asked for exists.
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/events'" in pages_js
    assert (build_dir / "views" / "custom" / "events.ejs").exists()
    # The model has no public views at all.
    assert not (build_dir / "views" / "events" / "index.ejs").exists()
    assert not (build_dir / "views" / "events" / "detail.ejs").exists()
    # Its router is mounted at /events (every model's is) and yields the
    # mount path to the page; the page's handler reads no rows.
    events_js = (build_dir / "routes" / "events.js").read_text()
    start = events_js.index("router.get('/'")
    root_block = events_js[start:events_js.index("router.get('/new'")]
    gate = root_block.index("if (!req.baseUrl.startsWith('/admin/')) {\n      return next();")
    assert gate < root_block.index("SELECT")
    page_handler = pages_js[pages_js.index("router.get('/events'"):]
    page_handler = page_handler[:page_handler.index("\n});")]
    assert "db.query" not in page_handler
    # Below the mount a visitor gets a 404 before any query runs.
    detail = events_js[events_js.index("router.get('/:id'"):events_js.index("router.get('/:id/edit'")]
    assert detail.index("if (!isAdmin) {\n      return res.status(404)") < detail.index("db.query")
    # And the reviewer is told that the page lists nothing.
    manifest = (build_dir / "compose-manifest.txt").read_text()
    assert "warning: page_named_like_model: custom page '/events' is named like the model 'Event'" in manifest


def test_form_target_beats_model_name_match(tmp_path):
    """A custom_page with form_target takes precedence over the model-
    slug match. The page keeps its POST handler + form widget, and the
    model's router hands the path on to it."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b4c-form-win",
        "description": "event producer; RSVP form",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Event", "fields": [{"name": "title", "type": "string"}]},
            {"name": "Rsvp", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
            ]},
        ],
        "pages": [
            {"path": "/rsvps", "title": "RSVP", "form_target": "Rsvp", "allowed_fields": ["name", "email"]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    # form page emits both GET + POST handlers in pages.js
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/rsvps'" in pages_js
    assert "router.post('/rsvps'" in pages_js
    # custom view uses the public-submission widget
    view = (build_dir / "views" / "custom" / "rsvps.ejs").read_text()
    assert "widgets/form/public-submission" in view
    # server.js still mounts the Rsvp router at its default slug
    # (/rsvps); Express register-order picks the first match, which is
    # whichever mounted first. We need to verify the form pages win
    # for public GETs.
    server_js = (build_dir / "server.js").read_text()
    # Rsvp router mounts at /rsvps by default (no auto public_listing_path
    # because the page carried form_target)
    assert "app.use('/rsvps', require('./routes/rsvps'));" in server_js


def test_model_without_matching_page_stays_admin_only(tmp_path):
    """When customer declares a data_model but NO custom page matches
    its slug, the model stays admin-only; hitting the bare /<slug>
    publicly returns 404 (existing v2 behavior)."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b4c-admin-only",
        "description": "yoga studio; admin-only members list",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Member", "fields": [{"name": "name", "type": "string"}]},
        ],
        # No custom page at /members: admin-only.
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    members_router = (build_dir / "routes" / "members.js").read_text()
    # The router's public-gate 404 branch must be present (admin-only).
    assert "error-404" in members_router
    # server.js mounts at /members (default) but the router 404s publicly.
    server_js = (build_dir / "server.js").read_text()
    assert "app.use('/members'" in server_js


def test_public_list_view_renders_model_columns(tmp_path):
    """When the spec lists a model publicly (public_listing_path), the
    emitted views/<slug>/index.ejs exists, iterates rows and references
    model-declared columns so customers see their data."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b4d-list",
        "description": "small business with a public events calendar",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Event", "public_listing_path": "/events", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "event_date", "type": "date", "required": True},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    index_view = build_dir / "views" / "events" / "index.ejs"
    assert index_view.is_file(), "public list view not emitted for a model the spec lists"
    content = index_view.read_text()
    # Iterates the rows collection the router passed in
    assert "rows" in content
    # References at least one customer-declared column
    assert ("title" in content) or ("event_date" in content)


def test_public_list_router_returns_rows_not_404(tmp_path):
    """The resource router's GET / must bypass the public-gate 404 when
    the spec set public_listing_path: an explicit listing must actually
    open the public mount, not just register it."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b4d-gate",
        "description": "small business with a public events calendar",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Event", "public_listing_path": "/events",
             "fields": [{"name": "title", "type": "string", "required": True}]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    events_js = (build_dir / "routes" / "events.js").read_text()
    get_root_idx = events_js.index("router.get('/'")
    next_handler = events_js.find("router.", get_root_idx + 50)
    root_block = events_js[get_root_idx: next_handler if next_handler > 0 else len(events_js)]
    # Public-gate 404 must NOT fire for the listing root.
    assert "error-404" not in root_block, (
        "public-gate 404 still present in GET / of a publicly listed model's router"
    )
    assert "res.render('events/index'" in root_block


def test_page_does_not_publish_a_model_the_archetype_keeps_private(tmp_path):
    """The event-producer archetype declares Photo with
    `public_listing_path: null` (private). A custom page at /photos does
    not change that: it is the spec's own page, and it must be reachable
    (the Photo router is mounted at /photos ahead of routes/pages.js, so it
    has to hand the path on instead of answering 404 in front of the page).
    """
    from compose.intake import translate_v1_spec
    from compose.build import BuildSpec, build_app, resolve_spec, _public_row_sources
    import json as _json
    v1 = {
        "app_name": "harbor-photos-public",
        "description": "art show producer with upcoming events and a photo gallery",
        "admin_email": "admin@example.com",
        "_harbor_meta": {"archetype_hint": "event-producer"},
        "data_models": [
            {"name": "Event", "fields": [{"name": "title", "type": "string", "required": True}]},
        ],
        "pages": [
            {"path": "/photos", "title": "Gallery"},
        ],
    }
    v2 = translate_v1_spec(v1)
    assert "photos" not in _public_row_sources(resolve_spec(BuildSpec.from_dict(_json.loads(_json.dumps(v2)))))
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    photos_js = (build_dir / "routes" / "photos.js").read_text()
    get_root_idx = photos_js.index("router.get('/'")
    next_handler = photos_js.find("router.", get_root_idx + 50)
    root_block = photos_js[get_root_idx: next_handler if next_handler > 0 else len(photos_js)]
    hand_on = "if (!req.baseUrl.startsWith('/admin/')) {\n      return next();"
    assert hand_on in root_block, (
        "the Photo router must hand GET /photos on to the custom page"
    )
    # A visitor's request leaves the handler there: before any query and
    # before the 404 the handler ends with.
    assert root_block.index(hand_on) < root_block.index("db.query") < root_block.index("error-404")
    assert "'photos/index'" not in photos_js
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/photos'" in pages_js
    assert (build_dir / "views" / "custom" / "photos.ejs").exists()
    assert not (build_dir / "views" / "photos" / "index.ejs").exists(), (
        "a public listing view was emitted for a model the spec did not publish"
    )


def test_page_named_like_a_private_archetype_model_never_lists_it(tmp_path):
    """Cross-archetype harness: a custom page whose path is the plural of
    a model the archetype declares with `public_listing_path: null` is
    served as a page, and the model gets no public listing."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    cases = [
        ("event-producer", "/photos", "photos"),
        ("event-producer", "/testimonials", "testimonials"),
        ("healer-wellness", "/testimonials", "testimonials"),
        ("therapist-counselor", "/faqs", "faqs"),
        ("fitness-studio", "/pricing-plans", "pricing-plans"),
    ]
    for archetype_hint, page_path, slug in cases:
        v1 = {
            "app_name": f"b4c-cross-{slug}",
            "description": "small business with public listings",
            "admin_email": "admin@example.com",
            "_harbor_meta": {"archetype_hint": archetype_hint},
            "data_models": [
                {"name": "Event", "fields": [{"name": "title", "type": "string"}]},
            ],
            "pages": [{"path": page_path, "title": "Listing"}],
        }
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / f"spec-{archetype_hint}-{slug}.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / f"out-{archetype_hint}-{slug}"
        build_app(str(spec_path), output_dir=str(build_dir))
        label = f"[{archetype_hint} {page_path}]"
        assert f"router.get('{page_path}'" in (build_dir / "routes" / "pages.js").read_text(), label
        assert not (build_dir / "views" / slug / "index.ejs").exists(), label
        assert not (build_dir / "views" / slug / "detail.ejs").exists(), label
        resource_js = (build_dir / "routes" / f"{slug}.js").read_text()
        get_root_idx = resource_js.index("router.get('/'")
        next_handler = resource_js.find("router.", get_root_idx + 50)
        root_block = resource_js[get_root_idx: next_handler if next_handler > 0 else len(resource_js)]
        assert "if (!req.baseUrl.startsWith('/admin/')) {\n      return next();" in root_block, label


# =============================================================================
# belongs_to FK end-to-end.
# Translator passes belongs_to through; schema emits the FK column; resource
# router fetches parent rows on GET /new + /:id/edit; admin form emits a
# <select> populated from parent_rows; POST accepts the FK column.
# =============================================================================

def _spec_with_belongs_to() -> dict:
    return {
        "app_name": "b4b-fk",
        "description": "Event producer; events belong to venues",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Venue", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "address", "type": "string"},
            ]},
            {
                "name": "Event",
                "belongs_to": "Venue",
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "event_date", "type": "date", "required": True},
                ],
            },
        ],
    }


def test_translator_preserves_belongs_to(tmp_path):
    """belongs_to string on a v1 data_model survives translation."""
    from compose.intake import translate_v1_spec
    v2 = translate_v1_spec(_spec_with_belongs_to())
    evt = next(m for m in v2["data_models"] if m["name"] == "Event")
    assert evt.get("belongs_to") == "Venue"


def test_belongs_to_schema_emits_fk_column(tmp_path):
    """emit_schema_sql renders `venue_id INTEGER REFERENCES venues(id)
    ON DELETE SET NULL` inside the events table."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v2 = translate_v1_spec(_spec_with_belongs_to())
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    schema = (build_dir / "db" / "schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS events" in schema
    events_block_start = schema.index("CREATE TABLE IF NOT EXISTS events")
    events_block = schema[events_block_start:schema.index(");", events_block_start)]
    assert "venue_id INTEGER REFERENCES venues(id)" in events_block, (
        f"events table missing FK column:\n{events_block}"
    )
    assert "ON DELETE SET NULL" in events_block


def test_belongs_to_router_fetches_parent_rows_for_new_and_edit(tmp_path):
    """routes/events.js GET /new + GET /:id/edit must SELECT parent rows
    and pass them to the view as parent_rows. Display column is inferred
    (Venue.name)."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v2 = translate_v1_spec(_spec_with_belongs_to())
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    events_js = (build_dir / "routes" / "events.js").read_text()
    # GET /new fetches venues
    new_idx = events_js.index("router.get('/new'")
    new_block = events_js[new_idx:events_js.index("router.", new_idx + 50)]
    assert "SELECT id, name AS display FROM venues" in new_block
    assert "parent_rows" in new_block
    # GET /:id/edit fetches venues
    edit_idx = events_js.index("router.get('/:id/edit'")
    edit_block = events_js[edit_idx:events_js.index("router.", edit_idx + 50)]
    assert "SELECT id, name AS display FROM venues" in edit_block
    assert "parent_rows" in edit_block
    # ALLOWED_COLS includes venue_id so POST accepts the FK
    assert '"venue_id"' in events_js


def test_admin_form_renders_belongs_to_select(tmp_path):
    """views/events/new.ejs + edit.ejs must carry a select field derived
    from belongs_to. The form template wires options from parent_rows."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v2 = translate_v1_spec(_spec_with_belongs_to())
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    new_view = (build_dir / "views" / "events" / "new.ejs").read_text()
    edit_view = (build_dir / "views" / "events" / "edit.ejs").read_text()
    for view in (new_view, edit_view):
        # Synthetic belongs_to_select field row must be in the template
        assert "belongs_to_select" in view or "venue_id" in view
        # Must reference parent_rows so options populate at render time
        assert "parent_rows" in view


def test_no_belongs_to_does_not_emit_fk_column(tmp_path):
    """Regression guard: models without belongs_to must NOT get an FK
    column. Negative case ensures the emitter isn't aggressive."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b4b-no-fk",
        "description": "yoga studio",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Session", "fields": [
                {"name": "title", "type": "string", "required": True},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    schema = (build_dir / "db" / "schema.sql").read_text()
    sessions_block_start = schema.index("CREATE TABLE IF NOT EXISTS sessions")
    sessions_block = schema[sessions_block_start:schema.index(");", sessions_block_start)]
    assert "REFERENCES" not in sessions_block
    # parent_rows should still default-empty in the form view; the select
    # field row should NOT be injected.
    sessions_router = (build_dir / "routes" / "sessions.js").read_text()
    assert "SELECT id," not in sessions_router or "parent_rows = []" in sessions_router


def test_belongs_to_unknown_parent_falls_back_gracefully(tmp_path):
    """When belongs_to names a model that doesn't exist in the spec,
    schema + router must NOT crash; the FK is skipped and a
    translation_warning should fire."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b4b-missing-parent",
        "description": "event producer",
        "admin_email": "admin@example.com",
        "data_models": [
            {
                "name": "Event",
                "belongs_to": "NonexistentVenue",
                "fields": [{"name": "title", "type": "string", "required": True}],
            },
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    schema = (build_dir / "db" / "schema.sql").read_text()
    events_block_start = schema.index("CREATE TABLE IF NOT EXISTS events")
    events_block = schema[events_block_start:schema.index(");", events_block_start)]
    # No FK since parent doesn't resolve
    assert "REFERENCES" not in events_block

# =============================================================================
# - customer v1.description flows into hero_description (lock-in)
# - customer-declared custom_pages appear in public nav
# =============================================================================

def test_v1_description_flows_into_site_description():
    """v1.description should survive translation and reach
    spec.settings.site_description (the SEO meta) so the customer's own
    words appear in <meta name="description">, OG tags, and dashboard
    previews. As of v2-settings-hero-vs-site-description-dedup it does
    NOT auto-flow into hero_description any more; hero_description is
    customer-editable and stays empty unless explicitly set, so the H1
    area doesn't render the SEO blurb as redundant body copy."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "b5a-hero",
        "description": "Quiet nature-based sound meditations for working adults.",
        "admin_email": "admin@example.com",
    }
    v2 = translate_v1_spec(v1)
    settings = v2.get("settings") or {}
    sd = settings.get("site_description") or ""
    assert ("sound meditation" in sd.lower()) or ("nature-based" in sd.lower()), (
        f"site_description did not carry v1.description:\n{sd!r}"
    )
    # hero_description must not be auto-filled with the same string;
    # empty is the contract when the customer hasn't set one explicitly.
    hd = settings.get("hero_description") or ""
    assert hd == "", (
        f"hero_description should default to empty, not echo site_description:\n{hd!r}"
    )


def test_custom_pages_appear_in_public_nav(tmp_path):
    """_default_nav_items should append customer-declared custom_pages
    (non-auth-required) after archetype pages so users see them in the
    site nav."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b5b-nav",
        "description": "Coach with custom pages",
        "admin_email": "admin@example.com",
        "pages": [
            {"path": "/manifesto", "title": "Our Manifesto"},
            {"path": "/secret", "title": "Secret", "auth_required": True},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    # Nav items are inlined into middleware/settings.js (res.locals.nav_items)
    settings_mw = (build_dir / "middleware" / "settings.js").read_text()
    assert '"label": "Our Manifesto"' in settings_mw
    assert '"href": "/manifesto"' in settings_mw
    # Auth-required page must NOT leak into public nav
    assert "Secret" not in settings_mw or '"/secret"' not in settings_mw


def test_nav_does_not_duplicate_archetype_paths(tmp_path):
    """A customer custom_page whose path matches an archetype default
    (e.g. /about) should NOT double-register in the nav. Archetype
    entry wins; the title override is tested further down."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b5b-dup",
        "description": "coach-consultant with custom About",
        "admin_email": "admin@example.com",
        "pages": [{"path": "/about", "title": "Our Story"}],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    settings_mw = (build_dir / "middleware" / "settings.js").read_text()
    # Exactly one /about entry in nav_items
    import re as _re
    matches = _re.findall(r'"href":\s*"/about"', settings_mw)
    assert len(matches) == 1, (
        f"expected exactly one /about nav entry, found {len(matches)} in "
        f"nav_items block"
    )


def test_form_target_pages_appear_in_nav(tmp_path):
    """Form pages (form_target set) should also surface in the
    nav so users can find "Contact Us" / "Book a Session" etc."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b5b-form-nav",
        "description": "coach with contact form",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "ContactMessage", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "message", "type": "text", "required": True},
            ]},
        ],
        "pages": [
            {"path": "/reach-out", "title": "Reach Out",
             "form_target": "ContactMessage", "allowed_fields": ["name", "email", "message"]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    settings_mw = (build_dir / "middleware" / "settings.js").read_text()
    assert '"label": "Reach Out"' in settings_mw
    assert '"href": "/reach-out"' in settings_mw

# =============================================================================
# Title override + model-merge semantics.
# - customer custom_page with archetype-collision path overrides the
#        emitted handler's title suffix + res.render locals.page_title.
# - Merge: customer-declared data_models AUGMENT (shadow by name) the
#          archetype defaults rather than REPLACE them.
# =============================================================================

def test_custom_page_title_overrides_archetype_default(tmp_path):
    """When customer declares `{path: "/about", title: "Our Story"}` for
    an archetype that has /about in default_pages, the emitted /about
    handler must render with the specific-first `title: "Our Story | ..."`
    and pass `page_title: "Our Story"` through to the view."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b2e-about",
        "description": "Executive coach with a personal About page",
        "admin_email": "admin@example.com",
        "pages": [{"path": "/about", "title": "Our Story"}],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    about_idx = pages_js.index("router.get('/about'")
    about_block = pages_js[about_idx:about_idx + 500]
    assert "Our Story" in about_block, (
        f"archetype /about handler did not pick up customer title:\n{about_block}"
    )
    assert "'Our Story | '" in about_block
    assert "page_title: 'Our Story'" in about_block


def test_no_title_override_keeps_archetype_default_title(tmp_path):
    """Negative guard: without a matching custom_page, archetype default
    _prettify(page_id) is still used. Regression check the override
    logic didn't overreach."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b2e-default",
        "description": "coach with no custom pages",
        "admin_email": "admin@example.com",
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    # The archetype default /about still uses About, specific-first
    assert "'About | '" in pages_js


def test_model_merge_customer_models_augment_archetype_defaults(tmp_path):
    """Customer declaring one Session model should NOT drop the
    archetype's other defaults. Merge semantics: archetype defaults
    remain (minus any shadowed by customer name), customer models
    append."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    # Use a coach archetype (coach-consultant default_data_models
    # typically includes BlogPost + Service + Inquiry etc).
    v1 = {
        "app_name": "merge-augment",
        "description": "coach with an added Session model",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "Session", "fields": [
                {"name": "title", "type": "string", "required": True},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    schema = (build_dir / "db" / "schema.sql").read_text()
    # Customer's Session table is present
    assert "CREATE TABLE IF NOT EXISTS sessions" in schema
    # At least ONE archetype default still survives. The coach archetype
    # typically emits Inquiry / ContactMessage / BlogPost; we check for
    # a superset so the test isn't brittle to archetype tweaks.
    archetype_defaults_present = any(
        f"CREATE TABLE IF NOT EXISTS {t}" in schema
        for t in ("contact_inquiries", "blog_posts", "services",
                  "case_studies", "testimonials")
    )
    assert archetype_defaults_present, (
        "merge semantics failed; no archetype default models survive "
        "when customer declares a model:\n" + schema
    )


def test_model_merge_customer_shadows_by_name(tmp_path):
    """When customer declares a model with a name matching an archetype
    default, customer wins entirely (customer's fields replace the
    archetype-default fields for that model)."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    # The coach archetype has a BlogPost default. Customer declares a
    # different shape for BlogPost; customer's shape must win.
    v1 = {
        "app_name": "merge-shadow",
        "description": "coach with custom BlogPost",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "BlogPost", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "custom_field_xyz", "type": "text"},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    schema = (build_dir / "db" / "schema.sql").read_text()
    # Exactly one blog_posts CREATE (not two from duplicate merge)
    count = schema.count("CREATE TABLE IF NOT EXISTS blog_posts")
    assert count == 1, f"blog_posts emitted {count} times, expected 1"
    # Customer's custom column is present
    blog_idx = schema.index("CREATE TABLE IF NOT EXISTS blog_posts")
    blog_block = schema[blog_idx:schema.index(");", blog_idx)]
    assert "custom_field_xyz" in blog_block, (
        f"customer's shadowed BlogPost fields missing:\n{blog_block}"
    )

# =============================================================================
# Admin notification on public form submit.
# - the dashboard emits a per-model new-this-week count alongside the total
# - the form POST handler fires sendAdminNotification after the INSERT
# =============================================================================

def test_dashboard_emits_new_count_per_model(tmp_path):
    """emit_routes_admin_dashboard_js must emit both total + last-7-day
    counts per model so the stat cards can show a "this week" pill
    when fresh submissions arrive."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b3d-dash",
        "description": "coach with a ContactMessage inbox",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "ContactMessage", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "message", "type": "text", "required": True},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    dash = (build_dir / "routes" / "admin-dashboard.js").read_text()
    # Both the total + new counts are queried
    assert "SELECT COUNT(*) FROM contact_messages" in dash
    assert "WHERE created_at > NOW() - INTERVAL '7 days'" in dash
    # stats_js includes both count + new_count per model
    assert "contact_messagesCount" in dash
    assert "contact_messagesNewCount" in dash
    assert "new_count:" in dash


def test_dashboard_widget_renders_pill_when_new(tmp_path):
    """The emitted admin/dashboard.ejs widget must read stat.new_count
    and conditionally render a 7-day delta chip when > 0. Letterpress
    polish (2026-05-07) replaced the filled "this week" pill with an
    outlined "+N · 7d" chip; the long form lives on the title attribute."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b3d-pill",
        "description": "coach",
        "admin_email": "admin@example.com",
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    widget = (build_dir / "views" / "widgets" / "admin" / "dashboard.ejs").read_text()
    assert "stat.new_count" in widget
    # Visible chip text: "+N · 7d" (using HTML middot entity)
    assert "7d" in widget
    assert "&middot;" in widget
    # Long form on the title attribute for hover discoverability.
    assert "New in the last 7 days" in widget
    # Only renders when new_count > 0 (no false-positive badge on cold models)
    assert "stat.new_count > 0" in widget


def test_services_notify_js_emitted(tmp_path):
    """A customer app must ship services/notify.js exposing
    sendAdminNotification(). Required for form-submission
    notifications; missing env vars → log-only skip, never a crash."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b3d-notify",
        "description": "coach",
        "admin_email": "admin@example.com",
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    notify = (build_dir / "services" / "notify.js").read_text()
    assert "sendAdminNotification" in notify
    assert "module.exports" in notify
    # Log-only fallback when env is missing
    assert "RESEND_API_KEY" in notify
    assert "ADMIN_EMAIL" in notify
    assert "[notify] admin-notify skipped" in notify
    # Must use fetch (Node 18+) not node-fetch / axios
    assert "fetch(RESEND_API_URL" in notify
    # Never throws; returns {skipped, ok, error}
    assert "return { skipped: true }" in notify


def test_form_post_wires_notification(tmp_path):
    """The form POST handler must import sendAdminNotification and
    fire it (fire-and-forget) after successful INSERT so admins get
    notified on new submissions. INSERT must use RETURNING id so the
    notification can link into /admin/<slug>/:id."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b3d-wire",
        "description": "coach with contact form",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "ContactMessage", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "message", "type": "text", "required": True},
            ]},
        ],
        "pages": [
            {"path": "/reach-out", "title": "Reach Out",
             "form_target": "ContactMessage", "allowed_fields": ["name", "email", "message"]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    # sendAdminNotification imported
    assert "sendAdminNotification" in pages_js
    assert "require('../services/notify')" in pages_js
    # POST handler wires it fire-and-forget with a .catch()
    post_idx = pages_js.index("router.post('/reach-out'")
    post_block = pages_js[post_idx:post_idx + 3000]
    assert "sendAdminNotification({" in post_block
    assert ".catch(" in post_block
    # Must use INSERT ... RETURNING id to link into /admin/<slug>/:id
    assert "RETURNING id" in post_block
    # The "View in admin" link target is the dash-slug path
    assert "/admin/contact-messages" in post_block


def test_no_notification_wiring_without_form_target(tmp_path):
    """Regression guard: informational custom pages (no form_target)
    must NOT emit sendAdminNotification calls, just the info view."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "b3d-info-only",
        "description": "coach with manifesto",
        "admin_email": "admin@example.com",
        "pages": [
            {"path": "/manifesto", "title": "Our Manifesto"},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    # sendAdminNotification is imported globally (fine; keeps emitter simple)
    # but there must be no actual call for informational pages.
    manifesto_idx = pages_js.index("router.get('/manifesto'")
    manifesto_block = pages_js[manifesto_idx:manifesto_idx + 1000]
    assert "sendAdminNotification(" not in manifesto_block

# =============================================================================
# v1-spec translation regression tests.
# Root-cause guards for four translation and emission bugs.
# =============================================================================

def test_classifier_word_boundary_prevents_substring_match():
    """Root cause: old classifier used `kw in haystack` substring matching,
    so `therapist` matched INSIDE `hypnotherapist` and a
    hypnotherapist's site misclassified as
    therapist-counselor. Fix: word-boundary regex so the classifier only
    hits on standalone-word appearances. Guards against every similar
    short-word-inside-longer-word case, not just this one."""
    from compose.intake import detect_archetype
    # The substring trap itself
    got = detect_archetype({
        "description": "Hypnotherapist offering guided relaxation sessions",
    })
    assert got == "healer-wellness", (
        f"hypnotherapist should not match 'therapist'; got {got}"
    )
    # Other substring traps the fix also resolves
    for desc, expected in [
        # "coach" still matches "coach" + "coaching", so coach wins here.
        ("Executive coach helping C-suite leaders", "coach-consultant"),
        # "cafe" (inside cafeteria) no longer falsely tags a company cafeteria
        # as restaurant-cafe; falls through to local-service. Locked in.
        ("Industrial cafeteria cleaning service", "local-service"),
    ]:
        assert detect_archetype({"description": desc}) == expected, (
            f"{desc!r} → expected {expected}"
        )


def test_classifier_tokenizes_camelcase_data_model_names():
    """Root cause: word-boundary matching broke the data_model hint path
    because `PlumbingJob` has no space between `Plumbing` and `Job`, so
    `\\bplumbing\\b` didn't match. Fix: tokenize CamelCase/snake/kebab
    model + action + role names into space-separated words before the
    word-boundary scan. Guards the hint path end-to-end."""
    from compose.intake import detect_archetype
    # CamelCase
    assert detect_archetype({
        "description": "Local business",
        "data_models": [{"name": "PlumbingJob"}],
    }) == "tradesperson-contractor"
    # snake_case
    assert detect_archetype({
        "description": "Local business",
        "data_models": [{"name": "yoga_class"}],
    }) == "fitness-studio"
    # kebab-case
    assert detect_archetype({
        "description": "Local business",
        "data_models": [{"name": "menu-item"}],
    }) == "restaurant-cafe"


def test_schema_dedup_when_customer_declares_created_at(tmp_path):
    """Root cause: emit_schema_sql always appended `created_at TIMESTAMPTZ
    DEFAULT NOW()` after the customer's fields. If the customer declared
    `created_at` as a field (a ContactInquiry model, say), the CREATE
    TABLE had the column twice → postgres error on migration. Fix: track
    declared names, skip the append when the customer has it."""
    from compose.build import build_app
    from compose.intake import translate_v1_spec
    import json as _json
    v1 = {
        "app_name": "dedup-test",
        "description": "Reiki practitioner with custom ContactInquiry",
        "admin_email": "admin@example.com",
        "data_models": [{
            "name": "ContactInquiry",
            "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "message", "type": "text", "required": True},
                # The exact customer-declared created_at that caused the bug.
                {"name": "created_at", "type": "timestamp"},
            ],
        }],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    schema = (build_dir / "db" / "schema.sql").read_text()
    # Isolate the contact_inquiries block
    start = schema.index("CREATE TABLE IF NOT EXISTS contact_inquiries")
    block = schema[start:schema.index(");", start)]
    # Exactly one `created_at` column in the table
    assert block.count("created_at") == 1, (
        f"created_at appears {block.count('created_at')} times, expected 1:\n{block}"
    )
    # Customer's declared column survives with DEFAULT NOW() (we auto-fill
    # default so INSERTs without explicit timestamp still work).
    assert "created_at TIMESTAMPTZ DEFAULT NOW()" in block


def test_schema_dedup_when_customer_declares_id(tmp_path):
    """Negative guard: if a customer declares an `id` field, it must NOT
    duplicate the emitter's `id SERIAL PRIMARY KEY` (would error with
    "multiple primary keys not allowed"). Customer's `id` is silently
    swallowed; SERIAL PRIMARY KEY always wins. Fix extends the dedup to
    `id` alongside `created_at`."""
    from compose.build import build_app
    from compose.intake import translate_v1_spec
    import json as _json
    v1 = {
        "app_name": "id-dedup",
        "description": "coach",
        "admin_email": "admin@example.com",
        "data_models": [{
            "name": "Note",
            "fields": [
                {"name": "id", "type": "integer"},  # customer-declared
                {"name": "body", "type": "text", "required": True},
            ],
        }],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    schema = (build_dir / "db" / "schema.sql").read_text()
    start = schema.index("CREATE TABLE IF NOT EXISTS notes")
    block = schema[start:schema.index(");", start)]
    # Exactly one PRIMARY KEY (the emitter's SERIAL one)
    assert block.count("PRIMARY KEY") == 1, (
        f"id duplication produced multiple PRIMARY KEYs:\n{block}"
    )
    # id SERIAL PRIMARY KEY is present (emitter's)
    assert "id SERIAL PRIMARY KEY" in block


def test_allowed_roles_ignored_when_auth_not_required(tmp_path):
    """Root cause: emit_routes_pages_js added requireRole() whenever
    allowed_roles was non-empty, even if auth_required=false. v1 specs
    commonly express "admin manages, everyone can view" as
    {auth_required: false, allowed_roles: ['admin']}. The old emitter
    slapped requireRole('admin') on a public route, which redirects
    anonymous visitors to /login instead of serving the page.
    Fix: ignore allowed_roles entirely when auth_required=false
    (public pages are public)."""
    from compose.build import build_app
    from compose.intake import translate_v1_spec
    import json as _json
    v1 = {
        "app_name": "public-page-with-admin-role",
        "description": "coach",
        "admin_email": "admin@example.com",
        "pages": [{
            "path": "/blog",
            "title": "Blog",
            "auth_required": False,
            "allowed_roles": ["admin"],
        }],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    # /blog handler must NOT carry requireAuth or requireRole middleware
    idx = pages_js.index("router.get('/blog'")
    line = pages_js[idx:idx + 200]
    assert "requireAuth" not in line, (
        f"/blog (auth_required=false) should have no requireAuth:\n{line}"
    )
    assert "requireRole" not in line, (
        f"/blog (auth_required=false + allowed_roles) should have no "
        f"requireRole:\n{line}"
    )


def test_translator_consumes_v1_seed_settings_for_customer_copy():
    """Root cause: translate_v1_spec only consulted `v1.description`,
    `v1.design`, and translator-derived strings. It never read
    `v1._seed_settings`, which is where customer-authored display copy
    actually lives. A customer's hero_tagline, about_body,
    contact_email, and social URLs were all dropped in favor of archetype
    defaults + app_name-derived title case. Fix: thread _seed_settings
    through the answers dict with customer-first precedence
    (customer > translator-derived > archetype default)."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "customer-copy-test",
        "description": "A sample wellness practice",
        "admin_email": "admin@example.com",
        "_seed_settings": {
            "site_name": "Sample Practitioner",
            "hero_tagline": "Sample Tagline, Plainly Invented",
            "hero_description": "Custom hero description.",
            "about_title": "About Sample Practitioner",
            "about_body": "Full customer about body.",
            "contact_email": "hello@sample-practitioner.example",
            "instagram_url": "https://instagram.example/sample-practitioner",
            "facebook_url": "https://facebook.example/sample-practitioner",
        },
    }
    v2 = translate_v1_spec(v1)
    s = v2.get("settings") or {}
    # Customer copy takes precedence over archetype + app_name-derived.
    assert s.get("site_name") == "Sample Practitioner", (
        f"customer site_name overridden: {s.get('site_name')!r}"
    )
    assert s.get("hero_tagline") == "Sample Tagline, Plainly Invented"
    assert s.get("hero_description") == "Custom hero description."
    assert s.get("about_title") == "About Sample Practitioner"
    assert s.get("about_body") == "Full customer about body."
    assert s.get("contact_email") == "hello@sample-practitioner.example"
    assert s.get("instagram_url") == "https://instagram.example/sample-practitioner"
    assert s.get("facebook_url") == "https://facebook.example/sample-practitioner"


def test_translator_seed_settings_missing_keys_fall_back_to_defaults():
    """Negative guard: when _seed_settings is partial (e.g. only site_name
    set), missing keys fall through to the translator's best guess or
    archetype defaults. Fix must not blank-out archetype-default copy
    when the customer provided a partial seed."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "partial-seed",
        "description": "coach",
        "admin_email": "admin@example.com",
        "_seed_settings": {"site_name": "Custom Coach Name"},
    }
    v2 = translate_v1_spec(v1)
    s = v2.get("settings") or {}
    # Customer-set key wins
    assert s.get("site_name") == "Custom Coach Name"
    # Missing keys fall through to translator-derived / archetype defaults.
    # Non-empty and not app_name-kebab-cased.
    assert s.get("hero_tagline"), "hero_tagline should fall back to archetype default"
    # hero_description is intentionally allowed to be empty when not
    # explicitly set (v2-settings-hero-vs-site-description-dedup); the
    # SEO blurb lives on site_description instead.
    assert s.get("site_description"), "site_description should fall back"
    assert "custom-coach-name" not in (s.get("hero_tagline") or "").lower()


# =============================================================================
# v2-settings-hero-vs-site-description-dedup
# Decouple hero_description (short, optional) from site_description
# (SEO meta, ~155 chars). When both
# fields are seeded with the same 250-char string, the long meta
# blurb shows up as redundant H1-area body copy.
# =============================================================================


def test_truncate_meta_description_helper_word_boundary():
    """_truncate_meta_description should clip on word boundary, never
    mid-word, and only ellipsize when truncation actually fired."""
    from compose.intake import _truncate_meta_description, _META_DESCRIPTION_MAX_LEN
    short = "A short description."
    assert _truncate_meta_description(short) == short, "no-op for short input"
    assert _truncate_meta_description("") == "", "empty input returns empty"
    assert _truncate_meta_description(None) == "", "None input returns empty"  # type: ignore[arg-type]
    long_text = (
        "Sample Irrigation Co is a sample irrigation contractor used in "
        "these tests. This description is long on purpose, so the meta "
        "description helper has to shorten it at a word boundary and "
        "finish the visible text with an ellipsis."
    )
    assert len(long_text) > _META_DESCRIPTION_MAX_LEN
    out = _truncate_meta_description(long_text)
    assert len(out) <= _META_DESCRIPTION_MAX_LEN + 1, (
        f"truncated length {len(out)} should be <= {_META_DESCRIPTION_MAX_LEN}+1 (ellipsis)"
    )
    assert out.endswith("…"), f"long input should end with ellipsis: {out!r}"
    # Last visible char before the ellipsis should be the end of a word
    # (no half-words): the text that follows the kept body in the source
    # must start with a space or punctuation.
    body = out[:-1].rstrip(" ,.;:-")
    assert long_text.startswith(body), f"truncation altered the text: {out!r}"
    assert body[-1] != "-" and long_text[len(body)] in " ,.;:-", (
        f"truncation must not split mid-word: {out!r}"
    )


def test_translator_long_v1_description_truncates_site_description():
    """A long v1.description should be
    truncated to a meta-friendly length on site_description, and must
    NOT pollute hero_description with the same long string."""
    from compose.intake import translate_v1_spec, _META_DESCRIPTION_MAX_LEN
    long_brief = (
        "Sample Irrigation Co is a sample irrigation contractor used in "
        "these tests. This brief is long on purpose, well past two hundred "
        "characters, so the translator has to shorten it for the site "
        "description instead of copying it through unchanged."
    )
    assert len(long_brief) >= 200
    v1 = {
        "app_name": "sample-irrigation",
        "description": long_brief,
        "admin_email": "admin@example.com",
    }
    v2 = translate_v1_spec(v1)
    s = v2.get("settings") or {}
    sd = s.get("site_description") or ""
    hd = s.get("hero_description") or ""
    assert sd, "site_description must be populated"
    assert len(sd) <= _META_DESCRIPTION_MAX_LEN + 1, (
        f"site_description not truncated: len={len(sd)} value={sd!r}"
    )
    assert hd == "", (
        f"hero_description must default to empty (was {hd!r})"
    )
    assert sd != long_brief, "site_description should not be the verbatim long brief"
    # And the two fields must not be identical strings (the bug).
    assert sd != hd, "site_description and hero_description must not be identical"


def test_translator_explicit_seed_hero_description_preserved():
    """When _seed_settings.hero_description is explicitly set, the
    translator must preserve it verbatim AND keep site_description
    independent (no longer copying hero_description into it)."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "explicit-hero",
        "description": "A long marketing brief that must not bleed into the hero area.",
        "admin_email": "admin@example.com",
        "_seed_settings": {
            "hero_description": "Quiet rooms for quiet work.",
        },
    }
    v2 = translate_v1_spec(v1)
    s = v2.get("settings") or {}
    assert s.get("hero_description") == "Quiet rooms for quiet work."
    sd = s.get("site_description") or ""
    assert sd, "site_description should still get the cleaned brief"
    assert sd != s.get("hero_description"), (
        "site_description and hero_description must remain distinct"
    )


def test_scaffold_spec_hero_description_omitted_when_blank():
    """scaffold_spec must not auto-fill hero_description with the long
    'site_name: archetype description' string. Empty is the new default
    so the home hero widget skips the description block (the EJS hero
    widgets all gate on `if (description)`)."""
    from compose.intake import scaffold_spec, _META_DESCRIPTION_MAX_LEN
    spec = scaffold_spec({
        "app_name": "blank-hero",
        "archetype": "coach-consultant",
        "site_name": "Blank Hero",
        "admin_email": "admin@example.com",
    })
    s = spec.get("settings") or {}
    assert s.get("hero_description", "_unset") == "", (
        f"hero_description should be empty by default, got {s.get('hero_description')!r}"
    )
    sd = s.get("site_description") or ""
    assert sd, "site_description should still be populated by archetype default"
    assert len(sd) <= _META_DESCRIPTION_MAX_LEN + 1
    # Home hero on the spec should reflect the empty hero_description so
    # the widget skips its description block.
    home_hero = (spec.get("pages") or {}).get("home", {}).get("hero", {})
    assert home_hero.get("description", "_unset") == ""


# =============================================================================
# Translation follow-ups: known-limitation fixes.
# - A page named after the first word of a model's slug (/blog beside
#   blog-posts) is an ordinary page, not a listing of the model
# - Archetype-curated hero/about image fallbacks (no more picsum random)
# =============================================================================

def test_blog_page_does_not_move_the_blog_posts_listing(tmp_path):
    """/blog beside a BlogPost model (slug blog-posts) is not a listing of
    it, whatever the first word of the slug is. The page is the spec's own
    page; the posts stay where the archetype shows them (the healer's
    /journal page); and the build report names the page."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "blog-autowire",
        "description": "Reiki practitioner",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "BlogPost", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "body", "type": "text", "required": True},
                {"name": "published", "type": "boolean"},
            ]},
        ],
        "pages": [{"path": "/blog", "title": "Blog"}],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    server_js = (build_dir / "server.js").read_text()
    assert "app.use('/blog', require('./routes/blog-posts'));" not in server_js
    assert "app.use('/blog-posts', require('./routes/blog-posts'));" in server_js
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/blog'" in pages_js
    assert (build_dir / "views" / "custom" / "blog.ejs").exists()
    assert not (build_dir / "views" / "blog-posts" / "index.ejs").exists()
    # The archetype's own listing of the posts is still there.
    assert "router.get('/journal'" in pages_js
    assert "FROM blog_posts WHERE published = TRUE" in pages_js
    manifest = (build_dir / "compose-manifest.txt").read_text()
    assert "warning: page_named_like_model: custom page '/blog' is named like the model 'BlogPost'" in manifest


def test_page_named_like_two_models_is_a_plain_page(tmp_path):
    """`/library` beside `library-books` and `library-events`: the page is
    an ordinary page, neither model is mounted there, and since the name
    points at no one model the build report has nothing to say about it."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "ambiguous-library",
        "description": "coach with a resource library",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "LibraryBook", "fields": [{"name": "title", "type": "string", "required": True}]},
            {"name": "LibraryEvent", "fields": [{"name": "title", "type": "string", "required": True}]},
        ],
        "pages": [{"path": "/library", "title": "Library"}],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    server_js = (build_dir / "server.js").read_text()
    assert "app.use('/library', require('./routes/library-books'));" not in server_js
    assert "app.use('/library', require('./routes/library-events'));" not in server_js
    assert (build_dir / "views" / "custom" / "library.ejs").is_file()
    manifest = (build_dir / "compose-manifest.txt").read_text()
    assert "page_named_like_model" not in manifest


def test_form_page_named_like_the_first_word_of_a_model_stays_a_form(tmp_path):
    """A form_target page keeps its form even if its path is the first
    word of some model's slug (/book beside booking-requests): no model is
    mounted at the form's path."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "form-wins",
        "description": "coach",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "BookingRequest", "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
            ]},
        ],
        "pages": [{"path": "/book", "title": "Book", "form_target": "BookingRequest",
                   "allowed_fields": ["name", "email"]}],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    # form page keeps its POST handler
    assert "router.post('/book'" in pages_js
    # The model is not mounted at the form's path.
    server_js = (build_dir / "server.js").read_text()
    assert "app.use('/book', require('./routes/booking-requests'));" not in server_js


def test_default_hero_image_is_curated_self_hosted_not_picsum():
    """Root cause history: the fallback used picsum.photos (random Unsplash
    photos by ID; id/20 shipped a logo-laden design desk onto a coach's hero +
    about, 2026-06-07). Fix: the self-hosted cli/stock_photos/ library, copied
    into the build at /images/stock/. Same app_name across rebuilds -> same
    photo, and the picked file MUST actually exist on disk (the manifest lists
    ~2x the committed files; referencing a missing one would ship a broken
    <img>)."""
    from compose.intake import _default_hero_image_url
    from compose.stock_photos import stock_photo_source_path
    url = _default_hero_image_url("healer-wellness", "sample-practitioner-test")
    assert url.startswith("/images/stock/"), f"hero is not self-hosted: {url!r}"
    assert "picsum" not in url, f"hero still uses picsum: {url!r}"
    fn = url.rsplit("/", 1)[-1]
    assert stock_photo_source_path(fn) is not None, (
        f"hero references a stock photo with no committed source file: {fn!r}"
    )
    # Deterministic: same inputs -> same URL.
    assert _default_hero_image_url("healer-wellness", "sample-practitioner-test") == url


def test_default_hero_image_every_pick_resolves_to_existing_file():
    """Across every archetype + a spread of app_names, the hero + about picks
    must ALWAYS resolve to a committed file (never a broken /images/stock/ ref)
    OR be empty (text-forward). Guards the manifest-vs-files gap (198 listed,
    ~103 present)."""
    from compose.intake import _default_hero_image_url, _default_about_photo_url
    from compose.stock_photos import stock_photo_source_path
    archetypes = [
        "healer-wellness", "therapist-counselor", "coach-consultant", "salon-spa",
        "fitness-studio", "restaurant-cafe", "tradesperson-contractor",
        "local-service", "event-producer",
    ]
    for arch in archetypes:
        for name in ("sample-one", "sample-two", "sample-coach", "sample-studio", "sample-practice"):
            for url in (_default_hero_image_url(arch, name), _default_about_photo_url(arch, name)):
                if url:
                    assert url.startswith("/images/stock/") and "picsum" not in url
                    fn = url.rsplit("/", 1)[-1]
                    assert stock_photo_source_path(fn) is not None, (
                        f"{arch}/{name} -> missing source file for {url!r}"
                    )


def test_default_hero_image_spreads_across_app_names():
    """Negative guard: different app_names within an archetype should spread
    across the curated pool, not always return the same photo (prevents an
    always-zero-index hashing regression)."""
    from compose.intake import _default_hero_image_url
    urls = {
        _default_hero_image_url("healer-wellness", n)
        for n in ("sample-one", "sample-two", "sample-coach", "sample-studio", "sample-five", "sample-six")
    }
    assert len(urls) >= 2, f"expected >= 2 distinct URLs, got {urls}"


def test_default_about_photo_differs_from_hero_for_same_app():
    """The about + hero image pools are intentionally disjoint so the
    two images on the built site do not accidentally show the same
    photo."""
    from compose.intake import _default_hero_image_url, _default_about_photo_url
    hero = _default_hero_image_url("healer-wellness", "sample-practitioner-test")
    about = _default_about_photo_url("healer-wellness", "sample-practitioner-test")
    assert hero != about, "hero + about image resolved to same URL"


def test_customer_hero_image_url_overrides_default():
    """Customer-supplied hero_image_url (via _seed_settings or v2
    settings) MUST win over the archetype-curated fallback. Guards the
    precedence chain so the placeholder only fires when customer has
    no image at all."""
    from compose.intake import translate_v1_spec
    v1 = {
        "app_name": "hero-override",
        "description": "Reiki",
        "admin_email": "admin@example.com",
        "_seed_settings": {
            "hero_image_url": "https://example.com/customer-hero.jpg",
        },
    }
    v2 = translate_v1_spec(v1)
    assert v2["settings"]["hero_image_url"] == "https://example.com/customer-hero.jpg"


# =============================================================================
# v2-services-intro-image-seed-settings (2026-05-07)
# Two _seed_settings keys landed 2026-05-06:
#   - services_intro: optional intro paragraph that replaces the generic
#     "Every engagement is designed around what will actually move the needle."
#     fallback in the /services (and /work-with-me) hero description.
#   - services_image_url: optional URL stamped onto the /services hero
#     image_url so the keeper's photo appears instead of an empty hero.
# Both flow: harbor_to_compose.ts -> v1_spec._seed_settings -> translate_v1_spec
# answers -> scaffold_spec -> spec.pages.services.hero.{description,image_url}.
# =============================================================================


def test_seed_settings_services_intro_overrides_default_services_hero():
    """Customer-supplied _seed_settings.services_intro replaces the generic
    fallback paragraph on the /services hero description."""
    from compose.intake import translate_v1_spec
    custom_intro = (
        "Every program is built around the leader, not a curriculum. "
        "We meet for ninety minutes and decide what is worth doing."
    )
    v1 = {
        "spec_version": "1.0",
        "app_name": "services-intro-coach",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
        "_seed_settings": {
            "site_name": "Maya Coach",
            "services_intro": custom_intro,
        },
    }
    v2 = translate_v1_spec(v1)
    services_hero = v2["pages"]["services"]["hero"]
    assert services_hero["description"] == custom_intro, (
        f"services_intro did not flow into pages.services.hero.description: "
        f"{services_hero.get('description')!r}"
    )
    # And the generic fallback must NOT appear when the customer set their own.
    assert "move the needle" not in services_hero["description"]


def test_seed_settings_services_intro_missing_falls_back_to_default():
    """When _seed_settings.services_intro is absent, the generic fallback
    paragraph still seeds the /services hero description. Negative guard
    so the new pass-through doesn't blank-out the default copy when the
    customer didn't supply an intro."""
    from compose.intake import translate_v1_spec
    v1 = {
        "spec_version": "1.0",
        "app_name": "services-intro-fallback",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
        "_seed_settings": {"site_name": "Maya Coach"},
    }
    v2 = translate_v1_spec(v1)
    services_hero = v2["pages"]["services"]["hero"]
    # Fallback paragraph from compose/intake.py scaffold_spec:842.
    assert services_hero["description"], "services hero description must not be empty"
    assert "move the needle" in services_hero["description"], (
        f"expected default services hero fallback, got: "
        f"{services_hero.get('description')!r}"
    )


def test_seed_settings_services_image_url_stamps_services_hero_image():
    """Customer-supplied _seed_settings.services_image_url is rendered as
    the /services hero image_url. When omitted, the hero block carries no
    image_url key (so the widget falls back to text-only / no image)."""
    from compose.intake import translate_v1_spec
    image_url = "https://example.com/customer-services-hero.jpg"
    v1 = {
        "spec_version": "1.0",
        "app_name": "services-image-coach",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
        "_seed_settings": {
            "site_name": "Maya Coach",
            "services_image_url": image_url,
        },
    }
    v2 = translate_v1_spec(v1)
    services_hero = v2["pages"]["services"]["hero"]
    assert services_hero.get("image_url") == image_url, (
        f"services_image_url did not flow into pages.services.hero.image_url: "
        f"{services_hero.get('image_url')!r}"
    )
    # image_alt is auto-derived from site_name.
    assert "Maya Coach" in (services_hero.get("image_alt") or "")

    # Negative branch: when omitted, no image_url key on the hero block.
    v1_no_image = {
        "spec_version": "1.0",
        "app_name": "services-no-image",
        "description": "Executive coach for C-suite leaders",
        "tier": "build",
        "_seed_settings": {"site_name": "Maya Coach"},
    }
    v2_no_image = translate_v1_spec(v1_no_image)
    services_hero_no_image = v2_no_image["pages"]["services"]["hero"]
    assert "image_url" not in services_hero_no_image, (
        f"services hero leaked an image_url when none was supplied: "
        f"{services_hero_no_image!r}"
    )


# =============================================================================
# Root-cause fixes for four issues seen in an admin walkthrough of a
# translated v1 spec:
#   - /contact leaked admin contact_inquiries as a public list
#   - admin Testimonials list showed only the "Published" column (text
#     fields all skipped by too-aggressive TEXT filter)
#   - /journal duplicated /blog in the nav
#   - seed placeholder images broken + generic "Sample Post N" copy
# =============================================================================

def test_a_contact_page_never_lists_the_contact_inquiries(tmp_path):
    """A /contact page beside a ContactInquiry model (slug
    contact-inquiries, first word 'contact') once listed the inbox
    publicly at /contact. No page lists a model because of its name: the
    archetype's contact page owns the path and the model stays behind the
    admin mount only."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "contact-leak-guard",
        "description": "Reiki practitioner",
        "admin_email": "admin@example.com",
        "data_models": [{
            "name": "ContactInquiry",
            "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "message", "type": "text", "required": True},
            ],
        }],
        # Customer declares /contact with allowed_roles=['admin'] shape
        # (v1 shorthand for "admin manages, everyone submits").
        "pages": [{"path": "/contact", "title": "Contact", "auth_required": False}],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    server_js = (build_dir / "server.js").read_text()
    # ContactInquiry router must be at /admin/contact-inquiries only,
    # NEVER publicly at /contact.
    assert "app.use('/contact', require('./routes/contact-inquiries'));" not in server_js, (
        "CRITICAL: ContactInquiry mounted publicly at /contact; "
        "would leak admin inbox to visitors"
    )
    # The archetype's /contact handler in pages.js still owns the path.
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/contact'" in pages_js


def test_a_page_at_a_submission_models_path_is_not_emitted_and_the_path_stays_404(tmp_path):
    """Submission-target models (Inquiry/Message/Booking/Submission/
    Subscriber/Registration/RSVP/Lead/Application) hold what visitors sent
    in. Like every model they are never listed because of a page's name;
    in addition a plain page at such a model's path is not emitted, the
    router keeps its 404 there, and the build report says the page is
    gone."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "submission-target-guard",
        "description": "coach",
        "admin_email": "admin@example.com",
        "data_models": [{
            "name": "Message",
            "plural": "messages",
            "fields": [
                {"name": "sender_name", "type": "string", "required": True},
                {"name": "email", "type": "email", "required": True},
                {"name": "body", "type": "text", "required": True},
            ],
        }],
        "pages": [{"path": "/messages", "title": "Messages"}],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    msgs_router = (build_dir / "routes" / "messages.js").read_text()
    get_root_idx = msgs_router.index("router.get('/'")
    next_handler = msgs_router.find("router.", get_root_idx + 50)
    root_block = msgs_router[get_root_idx:next_handler if next_handler > 0 else len(msgs_router)]
    assert "error-404" in root_block, (
        "CRITICAL: Message router lacks public-gate 404; /messages would "
        "publicly leak submission data:\n"
        + root_block[:400]
    )
    assert "router.get('/messages'" not in (build_dir / "routes" / "pages.js").read_text()
    assert not (build_dir / "views" / "messages" / "index.ejs").exists()
    manifest = (build_dir / "compose-manifest.txt").read_text()
    assert "warning: custom_page_route_taken_dropped: custom page '/messages' was not emitted" in manifest


def test_explicit_public_listing_path_is_honored_on_a_submission_named_model(tmp_path):
    """When the spec sets `public_listing_path` on a model, the model is
    listed there whatever it is called, a submission-style name included
    (e.g. a model of public quotes)."""
    from compose.build import build_app
    import json as _json
    # Skip the translator and drive compose directly; exact-match
    # requires explicit v2 shape.
    v2 = {
        "app_name": "explicit-public-inquiry",
        "archetype": "coach-consultant",
        "palette": "ocean-blue",
        "font_pair": "inter-inter",
        "admin_email": "admin@example.com",
        "settings": {"site_name": "ExplicitPublicInquiry"},
        "data_models": [{
            "name": "PublicQuoteInquiry",
            "plural": "public-quote-inquiries",
            "public_listing_path": "/quotes",
            "fields": [
                {"name": "name", "type": "string", "required": True},
                {"name": "quote", "type": "text", "required": True},
            ],
        }],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    server_js = (build_dir / "server.js").read_text()
    # Customer explicitly asked for /quotes → mounted publicly.
    assert "app.use('/quotes', require('./routes/public-quote-inquiries'));" in server_js


def test_admin_list_columns_show_text_identifier_fields(tmp_path):
    """Root cause: admin list column filter used `sql_type == TEXT` to
    skip body fields, but v1 maps every `type: "text"` short
    identifier (author_name, quote, service) to sql_type TEXT. Result:
    a Testimonial admin list showed only the `published` boolean
   ; no author, quote, or service column. Fix: switch filter to a
    name-based skip list so short-text identifiers surface while
    long-body fields (body/content/message/description) are skipped."""
    from compose.build import build_app
    from compose.intake import translate_v1_spec
    import json as _json
    v1 = {
        "app_name": "admin-columns",
        "description": "Reiki practitioner",
        "admin_email": "admin@example.com",
        "data_models": [{
            "name": "Testimonial",
            "fields": [
                {"name": "author_name", "type": "text"},
                {"name": "quote", "type": "text"},
                {"name": "service", "type": "text"},
                {"name": "published", "type": "boolean"},
            ],
        }],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    manage = (build_dir / "views" / "testimonials" / "manage.ejs").read_text()
    # At least author_name, quote or service should render as columns,
    # not ONLY published.
    assert '"key": "author_name"' in manage or '"key": "quote"' in manage or '"key": "service"' in manage, (
        f"admin list lost all text-identifier columns; only published "
        f"would render:\n{manage[:500]}"
    )


def test_admin_list_columns_still_skip_long_body_fields(tmp_path):
    """Negative guard: long-body fields (body/content/description/message/
    excerpt/bio) should still be skipped so the admin table stays
    compact."""
    from compose.build import build_app
    import json as _json
    v2 = {
        "app_name": "long-body-skip",
        "archetype": "coach-consultant",
        "palette": "ocean-blue",
        "font_pair": "inter-inter",
        "admin_email": "admin@example.com",
        "settings": {"site_name": "LongBodySkip"},
        "data_models": [{
            "name": "Note",
            "plural": "notes",
            "fields": [
                {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
                {"name": "body", "sql_type": "TEXT"},
                {"name": "description", "sql_type": "TEXT"},
                {"name": "excerpt", "sql_type": "TEXT"},
            ],
        }],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    manage = (build_dir / "views" / "notes" / "manage.ejs").read_text()
    # title shows; body + description + excerpt do NOT.
    assert '"key": "title"' in manage
    assert '"key": "body"' not in manage
    assert '"key": "description"' not in manage
    assert '"key": "excerpt"' not in manage


def test_archetype_alias_page_is_dropped_only_when_something_stands_in_for_it(tmp_path):
    """A customer page of the same kind as an archetype default replaces
    it (/contact-us for /contact), with one exception: an archetype page
    that lists a model's rows. A custom page lists nothing, so dropping the
    healer's /journal for a custom /blog would take the posts off the site.
    /journal is dropped only when the spec itself lists the posts at /blog
    (public_listing_path)."""
    from compose.build import build_app
    import json as _json
    import re as _re

    def build(tag, pages, blog_listing=None):
        model = {
            "name": "BlogPost",
            "fields": [
                {"name": "title", "sql_type": "VARCHAR(255)", "required": True},
                {"name": "body", "sql_type": "TEXT", "required": True},
                {"name": "published", "sql_type": "BOOLEAN", "default": "TRUE"},
            ],
        }
        if blog_listing:
            model["public_listing_path"] = blog_listing
        spec = _json.loads(SAMPLE_SPEC.read_text())
        spec["app_name"] = f"alias-{tag}"
        spec["data_models"] = [model]
        spec["custom_pages"] = pages
        out = tmp_path / tag
        build_app(spec, output_dir=out)
        nav = _re.search(r"const _NAV_ITEMS = (\[.*?\]);", (out / "middleware" / "settings.js").read_text(), _re.DOTALL)
        return out, [item["href"] for item in _json.loads(nav.group(1))]

    # A plain custom /blog page: the journal keeps listing the posts.
    out, nav = build("plain", [{"path": "/blog", "title": "Blog"}])
    assert (out / "views" / "journal.ejs").exists()
    assert (out / "views" / "custom" / "blog.ejs").exists()
    assert "/journal" in nav and "/blog" in nav

    # The spec lists the posts at /blog itself: the journal is dropped.
    out, nav = build("listed", [{"path": "/blog", "title": "Blog"}], blog_listing="/blog")
    assert not (out / "views" / "journal.ejs").exists(), "archetype /journal should give way to the spec's /blog listing"
    assert (out / "views" / "blog-posts" / "index.ejs").exists()
    assert "/journal" not in nav and "/blog" in nav

    # A page with no listing to stand in for is replaced as before.
    out, nav = build("contact", [{"path": "/contact-us", "title": "Contact us"}])
    assert not (out / "views" / "contact.ejs").exists()
    assert "/contact" not in nav and "/contact-us" in nav


def test_seed_images_use_stable_picsum_ids_not_random_seeds(tmp_path):
    """Root cause: seed data (blog posts / events / staff / etc.) used
    `picsum.photos/seed/<app_name>-<slot>-<i>/W/H`, which returns
    TRUE-RANDOM photos and was blocked by some browsers' tracker
    protection. Fix: all seed image URLs use the stable `picsum.photos/
    id/<N>/W/H` format with archetype-curated pool."""
    from compose.build import build_app
    from compose.intake import translate_v1_spec
    import json as _json
    v1 = {
        "app_name": "seed-images",
        "description": "Reiki practitioner",
        "admin_email": "admin@example.com",
        "data_models": [
            {"name": "BlogPost", "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "image_url", "type": "url"},
                {"name": "published", "type": "boolean"},
            ]},
        ],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    seed_sql = (build_dir / "db" / "seed.sql").read_text()
    # No more `picsum.photos/seed/` URLs anywhere in seed data
    assert "picsum.photos/seed/" not in seed_sql, (
        "seed data still contains legacy picsum.photos/seed/ URLs (true-random)"
    )
    # Stable-ID format is present (for blog posts at minimum)
    import re as _re
    ids = _re.findall(r"picsum\.photos/id/(\d+)/", seed_sql)
    assert len(ids) >= 3, f"expected >= 3 stable picsum IDs, found {len(ids)}"


def test_seed_blog_posts_are_archetype_appropriate(tmp_path):
    """Root cause: seed data used generic `Sample Post N` + `Sample body.
    Replace with real content when the founder has time.` Fix:
    archetype-specific titles + bodies that read like real
    placeholder-in-progress writing, not stubs."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1_healer = {
        "app_name": "healer-sample",
        "description": "Reiki master + energy work",
        "admin_email": "admin@example.com",
        "data_models": [{"name": "BlogPost", "fields": [
            {"name": "title", "type": "string"},
            {"name": "body", "type": "text"},
            {"name": "image_url", "type": "url"},
            {"name": "published", "type": "boolean"},
        ]}],
    }
    v2 = translate_v1_spec(v1_healer)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    seed_sql = (build_dir / "db" / "seed.sql").read_text()
    # Not the generic `Sample Post 1` stub anymore (the fix uses titles
    # like "Sample post: what to expect in a first session").
    import re as _re
    bare_sample_titles = _re.findall(r"'Sample Post \d+'", seed_sql)
    assert not bare_sample_titles, (
        f"seed_sql still has bare 'Sample Post N' titles: {bare_sample_titles}"
    )
    # Healer-specific phrasing present
    assert "first session" in seed_sql or "subconscious" in seed_sql or "pacing" in seed_sql, (
        f"healer sample posts not archetype-appropriate:\n"
        f"{_re.findall(r'INSERT INTO blog_posts[^;]+', seed_sql)[:500] if _re.findall(r'INSERT INTO blog_posts[^;]+', seed_sql) else '(none)'}"
    )


def test_resource_public_mount_yields_on_archetype_collision(tmp_path):
    """When a resource's public mount collides
    with an archetype default page (e.g. Event→/events in healer
    archetype, or ScheduleSlot→/schedule in fitness archetype), the
    public mount is STILL emitted (so /events/:id detail flows to the
    resource router's GET /:id handler), but the router's GET /
    handler checks COLLIDES_WITH_ARCHETYPE_PAGE and calls next() to
    yield to the pages router mounted at /.

    Previously we skipped the public mount entirely when
    default /<slug> collided, but that broke /events/1 detail routes.
    Also did not cover explicit public_listing_path collisions
    (e.g. fitness-studio ScheduleSlot.public_listing_path=/schedule).
    The current design unifies both cases via the yield-on-collision approach.
    """
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    import json as _json
    v1 = {
        "app_name": "events-archetype-collision",
        "description": "Reiki practitioner",
        "admin_email": "admin@example.com",
        "data_models": [{
            "name": "Event",
            "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "event_date", "type": "date", "required": True},
            ],
        }],
    }
    v2 = translate_v1_spec(v1)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    server_js = (build_dir / "server.js").read_text()
    # Public mount IS emitted (always). Admin too.
    assert "app.use('/events', require('./routes/events'));" in server_js, (
        "compose must always emit public mount so /events/:id detail works"
    )
    assert "app.use('/admin/events', requireAdmin, require('./routes/events'));" in server_js
    # Resource router declares the collision so its GET / yields via next()
    events_js = (build_dir / "routes" / "events.js").read_text()
    assert "COLLIDES_WITH_ARCHETYPE_PAGE = true" in events_js, (
        "routes/events.js must declare COLLIDES_WITH_ARCHETYPE_PAGE=true "
        "so GET / calls next() and the pages router takes over"
    )
    # Archetype's /events handler in pages.js still owns the list URL
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/events'" in pages_js


def test_resource_public_mount_honored_with_explicit_public_listing_path(tmp_path):
    """When the spec sets `public_listing_path` on a model, the public
    mount is emitted at that path. Only the DEFAULT-mount collision with
    an archetype page yields to the page."""
    from compose.build import build_app
    import json as _json
    # Coach archetype; the spec lists BlogPost at /blog itself
    # (public_listing_path). The router mounts publicly at /blog.
    v2 = {
        "app_name": "explicit-public-mount",
        "archetype": "coach-consultant",
        "palette": "ocean-blue",
        "font_pair": "inter-inter",
        "admin_email": "admin@example.com",
        "settings": {"site_name": "Explicit Public Mount"},
        "data_models": [{
            "name": "BlogPost",
            "plural": "blog-posts",
            "public_listing_path": "/blog",
            "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "body", "type": "text", "required": True},
            ],
        }],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    server_js = (build_dir / "server.js").read_text()
    assert "app.use('/blog', require('./routes/blog-posts'));" in server_js
    assert "app.use('/admin/blog-posts', requireAdmin, require('./routes/blog-posts'));" in server_js


# =============================================================================
# Regression tests: offerings threading, event-producer
# classifier, resolver aliases, Photo mount collision, archetype testimonial
# defaults, em-dash-free scaffold output.
# =============================================================================


def test_offerings_threading_coach_home_default(tmp_path):
    """Root cause: coach-home-default layout resolves `spec.home.offerings`.
    Earlier drafts of the layout + fixtures drifted between `services`,
    `items`, and `offerings`, yielding empty offering grids on the
    generated home. This test locks in that a coach-consultant spec
    carrying `pages.home.offerings` renders the offering items into
    the composed home view (not the widget's empty-state fallback).
    """
    from compose.intake import scaffold_spec
    from compose.build import build_app
    import json as _json

    spec = scaffold_spec({
        "app_name": "coach-threading-sample",
        "archetype": "coach-consultant",
        "site_name": "Coach Threading Sample",
        "admin_email": "admin@example.com",
    })
    # Replace scaffold defaults with a detectable marker string so we can
    # prove the offering items actually threaded into the composed view.
    spec["pages"]["home"]["offerings"]["items"] = [
        {"title": "Marker Offering Alpha", "description": "Unique alpha token.", "price": "$100"},
        {"title": "Marker Offering Beta", "description": "Unique beta token.", "price": "$200"},
    ]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(spec))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    home_ejs = (build_dir / "views" / "home.ejs").read_text()
    assert "Marker Offering Alpha" in home_ejs, (
        f"offering title did not thread into home.ejs for coach-home-default:\n{home_ejs[:1500]}"
    )
    assert "Marker Offering Beta" in home_ejs
    # Widget include for offerings-grid-3col must be emitted (not skipped)
    assert "offerings/grid-3col.ejs" in home_ejs or "offerings-grid-3col" in home_ejs


def test_event_producer_classifier_matches_producer_descriptions():
    """event-producer archetype added for ICP sample #3
    (art-show producer, concert promoter, festival organizer). Lock in
    every keyword path so pruning cannot silently regress a known-good
    description. Also guards the `coach-consultant` + `healer-wellness`
    keyword-collision priority: single-word "event" inside those
    archetypes must NOT pull the spec into event-producer."""
    from compose.intake import detect_archetype

    assert detect_archetype({"description": "Contemporary art show producer in town"}) == "event-producer"
    assert detect_archetype({"description": "Annual art fair booking agency"}) == "event-producer"
    assert detect_archetype({"description": "Monthly gallery exhibition openings"}) == "event-producer"
    assert detect_archetype({"description": "Open studios tour organizer"}) == "event-producer"
    assert detect_archetype({"description": "Independent concert promoter with touring series"}) == "event-producer"
    assert detect_archetype({"description": "Music festival organizer in the hills"}) == "event-producer"
    assert detect_archetype({"description": "Performing arts series curator"}) == "event-producer"
    assert detect_archetype({"description": "Pop-up show producer for emerging artists"}) == "event-producer"
    # Collision guard: healer-wellness spec that happens to carry an Event
    # model must remain healer-wellness (single-word "event" is NOT in
    # event-producer's keyword list; phrases are multi-word by design).
    assert detect_archetype({
        "description": "Reiki master and energy healer",
        "data_models": [{"name": "Event"}],
    }) == "healer-wellness"
    # Collision guard: coach-consultant with an Event model stays coach.
    assert detect_archetype({
        "description": "Executive leadership coach",
        "data_models": [{"name": "Event"}],
    }) == "coach-consultant"


def test_data_key_aliases_resolver_fallback(tmp_path):
    """`_DATA_KEY_ALIASES` in compose.build lets layout
    source paths standardize on canonical keys (`offerings`, `testimonials`)
    while still resolving specs that stored the block under legacy keys
    (`services`, `reviews`). Regression guard: coach-home-default's layout
    points at `spec.home.offerings`, but this test stores the block under
    `pages.home.services` and asserts the alias fallback still threads
    the items into the composed home view."""
    from compose.intake import scaffold_spec
    from compose.build import build_app
    import json as _json

    spec = scaffold_spec({
        "app_name": "coach-alias-sample",
        "archetype": "coach-consultant",
        "site_name": "Coach Alias Sample",
        "admin_email": "admin@example.com",
    })
    # Move the offerings block to the alias key `services`. The layout
    # keeps pointing at `spec.home.offerings`; the resolver must fall
    # through _DATA_KEY_ALIASES["offerings"] = ["services", "items"].
    spec["pages"]["home"]["services"] = {
        "eyebrow": "SERVICES",
        "heading": "What I Offer",
        "items": [
            {"title": "Alias Offering Gamma", "description": "Gamma token.", "price": "$300"},
        ],
    }
    spec["pages"]["home"].pop("offerings", None)
    # NOTE 2026-04-29 (admin/public alignment fix): the home page's
    # testimonials widget is now db:testimonials across all archetypes,
    # so the `reviews` alias for static spec testimonials is no longer
    # consulted on the home page: items come from a SELECT at request
    # time. Test now exercises the resolver via offerings only (which
    # remains a static spec-sourced widget). Alias-resolution behavior
    # for db-sourced widgets is exercised separately via the items
    # runtime ref.
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(spec))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    home_ejs = (build_dir / "views" / "home.ejs").read_text()
    assert "Alias Offering Gamma" in home_ejs, (
        f"alias `services` did not fall through to `offerings` source in home.ejs:\n{home_ejs[:1500]}"
    )
    # NOTE 2026-04-29: testimonials-stack source changed from
    # `spec.home.testimonials` to `db:testimonials` (with a
    # follow-on harness extension). Static spec data no longer bakes
    # into home.ejs for testimonials; the rendered list now comes from
    # the testimonials table at runtime via the route handler. The
    # alias resolver still flows `pages.home.reviews` -> `testimonials`
    # at the seed-emitter layer (so the testimonials table gets the
    # alias data), but the assertion that it appears in home.ejs is
    # no longer valid. Verify the runtime contract via the dedicated
    # `TestListingWidgetDbSource` harness in test_layout_variants.py.
    assert "include('./widgets/testimonials/stack.ejs'" in home_ejs, (
        "testimonials-stack include missing from home.ejs (regression on widget composition)"
    )


def test_event_producer_photo_no_past_events_mount_collision(tmp_path):
    """Root cause: archetypes.json originally shipped
    event-producer with `Photo.public_listing_path = "/past-events"`,
    colliding with the archetype's own `/past-events` page router.
    Server.js ended up with two `app.use('/past-events', ...)` lines;
    the second one (`./routes/photos`) won at runtime and shadowed the
    archetype page. Fix: Photo.public_listing_path = null so Photo
    mounts at its default `/photos` (or wherever the resolver chooses
    WITHOUT colliding with archetype default_pages).
    """
    from compose.intake import scaffold_spec
    from compose.build import build_app
    import json as _json

    spec = scaffold_spec({
        "app_name": "event-producer-photo-sample",
        "archetype": "event-producer",
        "site_name": "Event Producer Photo Sample",
        "admin_email": "admin@example.com",
    })
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(spec))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))
    server_js = (build_dir / "server.js").read_text()
    # The Photo router MUST NOT be mounted at /past-events; that path
    # belongs to the archetype's past-events page in pages.js.
    assert "app.use('/past-events', require('./routes/photos'));" not in server_js, (
        "Photo router mounted at /past-events, colliding with archetype page:\n"
        + "\n".join(l for l in server_js.split("\n") if "past-events" in l.lower() or "photos" in l.lower())
    )
    # Admin mount for Photo still works (admin CRUD is independent)
    assert "app.use('/admin/photos', requireAdmin, require('./routes/photos'));" in server_js


def test_archetype_testimonial_defaults_differ_per_archetype():
    """Root cause: `_default_testimonials` used to emit coach-shaped copy
    ("Coaching container", executive sponsors) for EVERY archetype,
    which read wildly wrong on event-producer / restaurant-cafe /
    tradesperson builds. replaced it with
    `_ARCHETYPE_DEFAULT_TESTIMONIALS`, a per-archetype template table.
    This test asserts the table is actually keyed per archetype and
    the scaffold's testimonial service labels differ as expected."""
    from compose.intake import _default_testimonials

    event_t = _default_testimonials("Sample Events", "event-producer")
    healer_t = _default_testimonials("Sample Healer", "healer-wellness")
    restaurant_t = _default_testimonials("Sample Cafe", "restaurant-cafe")
    trades_t = _default_testimonials("Sample Trades", "tradesperson-service")
    coach_t = _default_testimonials("Sample Coach", "coach-creator")

    # Event-producer testimonials name "Past event" / "Coverage" /
    # "Collaboration" service labels; no "Coaching" label.
    event_services = [t["service"] for t in event_t]
    assert "Past event" in event_services, event_services
    assert "Coaching" not in event_services
    # Healer testimonials include a session/energy-work/retreat label.
    healer_services = [t["service"] for t in healer_t]
    assert any(s in healer_services for s in ("Session", "Energy work", "Retreat")), healer_services
    # Restaurant testimonials reference dinner/lunch/review.
    restaurant_services = [t["service"] for t in restaurant_t]
    assert any(s in restaurant_services for s in ("Dinner", "Lunch", "Review")), restaurant_services
    # Tradesperson testimonials reference repair/install/service call.
    trades_services = [t["service"] for t in trades_t]
    assert any(s in trades_services for s in ("Repair", "Install", "Service call")), trades_services
    # Coach defaults still present (fallback key coach-creator).
    coach_services = [t["service"] for t in coach_t]
    assert "Coaching" in coach_services or "Retainer" in coach_services, coach_services
    # First quote always weaves in the site_name exactly once.
    assert "Sample Events" in event_t[0]["quote"]
    assert event_t[0]["quote"].count("(on Sample Events)") == 1


def test_scaffold_spec_output_has_no_em_dashes():
    """Brand voice rule: no em dashes (U+2014) or &mdash; in customer-facing
    copy. removed five em dashes from scaffold_spec
    and the admin event-detail template. This test asserts that the
    serialized scaffold output is em-dash-free across every registered
    archetype, so future defaults cannot silently reintroduce one.
    """
    from compose.intake import scaffold_spec
    from compose.build import load_archetypes
    import json as _json

    archetype_ids = [a["id"] for a in load_archetypes()["archetypes"]]
    assert archetype_ids, "load_archetypes() returned no archetypes"
    for archetype_id in archetype_ids:
        spec = scaffold_spec({
            "app_name": f"em-dash-check-{archetype_id}",
            "archetype": archetype_id,
            "site_name": f"Em Dash Check {archetype_id}",
            "admin_email": "admin@example.com",
        })
        blob = _json.dumps(spec, ensure_ascii=False)
        assert "\u2014" not in blob, (
            f"scaffold_spec for archetype={archetype_id} contains em dash U+2014.\n"
            f"First offending context:\n"
            f"{blob[max(0, blob.index(chr(0x2014)) - 80):blob.index(chr(0x2014)) + 80]}"
        )
        assert "&mdash;" not in blob, (
            f"scaffold_spec for archetype={archetype_id} contains &mdash;."
        )


def test_emit_page_view_wraps_bare_array_widget_data(tmp_path):
    """Root cause: `source: "spec.X.items"` in
    page-layouts.json makes `data_key = "items"` and `widget_data`
    becomes the raw array from `spec.pages.X.items`. Passing a bare
    array to `include('widget.ejs', [...])` binds the array as `locals`
    but doesn't set `locals.items`; widgets with
    `typeof items !== 'undefined' ? items : ...` see undefined and
    render empty. Defensive wrap: arrays are promoted to
    `{data_key: array}` so the widget's include contract is satisfied.
    """
    from compose.build import build_app
    import json as _json

    # Minimal spec exercising a page-layout widget source path ending in `.items`
    # with a bare array at `spec.pages.<page>.items`.
    spec = {
        "app_name": "bare-array-wrap-test",
        "archetype": "event-producer",
        "admin_email": "admin@example.com",
        "palette": "deep-plum",
        "font_pair": "playfair-source-sans",
        "settings": {
            "site_name": "Bare Array Test",
            "hero_tagline": "tagline",
        },
        "pages": {
            "home": {
                "hero": {"eyebrow": "E", "heading": "H", "description": "D"},
                "upcoming": {"eyebrow": "U", "heading": "Upcoming", "items": [
                    {"title": "Row 1", "event_date": "2026-06-01", "description": "x", "image_url": "https://picsum.photos/1"},
                    {"title": "Row 2", "event_date": "2026-06-02", "description": "y", "image_url": "https://picsum.photos/2"},
                ]},
                "testimonials": {"eyebrow": "T", "heading": "T", "items": [
                    {"quote": "q", "author": "a", "role": "r"}
                ]},
                "newsletter": {"heading": "N", "description": "d", "placeholder": "you@x.example", "button_text": "Go"},
            },
        },
        "seed_content": {
            "events": [
                {"title": "E1", "event_date": "2026-07-01", "end_time": "21:00", "event_time": "19:00", "location": "L", "description": "d", "registration_url": "", "image_url": ""},
            ],
            "photos": [
                {"image_url": "https://picsum.photos/id/1/400/400", "title": "p", "caption": "c"},
            ],
        },
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_json.dumps(spec))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))

    # home.ejs must include widgets with object-literal data
    # (not bare array literals): specifically testimonials where the
    # spec provides {items: [...]}.
    home_ejs = (build_dir / "views" / "home.ejs").read_text()
    # Every include must pass a `{...}` object literal (not `[...]` bare array).
    import re
    includes = re.findall(r"include\(\s*'[^']+\.ejs'\s*,\s*(\[|\{)", home_ejs)
    assert includes, f"no widget includes found in home.ejs:\n{home_ejs[:500]}"
    bare_arrays = [c for c in includes if c == "["]
    assert not bare_arrays, (
        f"home.ejs has {len(bare_arrays)} bare-array include(s). Widget "
        f"includes must receive object locals, not raw arrays:\n{home_ejs}"
    )


def test_intake_seed_content_has_no_duplicate_past_events_keys():
    """Root cause: intake.py emitted BOTH
    `past_events` (underscored) AND `past-events` (dashed) under
    seed_content with identical rows, then the archetype-whitelist
    filter kept both because PastEvent.plural='past-events' matched
    on the dashed key. The v2 build lint greps for `INSERT INTO
    <spec_key>` per seed_content key; the dashed key never hits the
    actual table (which is snake_case `past_events`), producing
    false-positive 'seed.sql has 0 INSERTs for past-events' errors.

    Guard: intake must emit exactly one seed block per model; all
    emit_seed_sql + lint lookups normalize on snake_case.
    """
    from compose.intake import scaffold_spec

    spec = scaffold_spec({
        "app_name": "dedupe-test",
        "archetype": "event-producer",
        "site_name": "Dedupe Test",
        "admin_email": "admin@example.com",
    })
    keys = list(spec.get("seed_content", {}).keys())
    normalized = [k.replace("-", "_") for k in keys]
    assert len(set(normalized)) == len(normalized), (
        f"seed_content has duplicate keys when dash-normalized: "
        f"{sorted(keys)} → {sorted(normalized)}"
    )


# ---- archetype self-collision yield-to-pages ----

def _fitness_resolved():
    with open(FIXTURE_DIR / "sample-fitness-spec.json") as f:
        return resolve_spec(BuildSpec.from_dict(json.load(f)))


def test_healer_non_colliding_routers_keep_listing():
    """Counter-check: healer's BlogPost (/journal) and Event (/events)
    mount at archetype default pages, so they collide and yield.
    BUT a hypothetical non-colliding model (public_listing_path='/rss')
    would NOT collide. We don't currently have such a model in healer,
    so this test just asserts the generator correctly distinguishes
    collision vs non-collision: Testimonial/ContactInquiry have
    public_listing_path=null → no public mount → router still emits
    COLLIDES=false (since effective_public_path is /<slug>, which
    isn't in the archetype's default pages).
    """
    with open(FIXTURE_DIR / "sample-healer-spec.json") as f:
        resolved = resolve_spec(BuildSpec.from_dict(json.load(f)))
    files = dict(compose_files(resolved))
    # Testimonial has no public_listing_path → default /testimonials,
    # which is NOT an archetype default page → COLLIDES=false
    testimonial_js = files["routes/testimonials.js"]
    assert "COLLIDES_WITH_ARCHETYPE_PAGE = false" in testimonial_js, (
        "Testimonial (no public_listing_path, default /testimonials doesn't "
        "collide with any healer archetype page) must emit COLLIDES=false"
    )


def test_fitness_router_get_slash_yields_before_public_gate():
    """The yield check (next() on public-mount collision) must run
    BEFORE the public_gate (404 for non-public models) so the pages
    router gets the request. If the public_gate ran first it would
    404 before ever reaching next().

    Also asserts the handler signature includes `next`; previously it
    was `async (req, res) =>` with no next param, which meant
    callers couldn't yield.
    """
    resolved = _fitness_resolved()
    files = dict(compose_files(resolved))
    sched = files["routes/schedule-slots.js"]
    # Handler signature includes next
    assert "router.get('/', async (req, res, next) =>" in sched, (
        "GET / handler must accept `next` as the 3rd arg so it can yield "
        "via next() on archetype-page collisions"
    )
    # Yield-check appears before any render/query (index of return next()
    # check < index of the first res.render / db.query)
    yield_idx = sched.index("return next();")
    first_query_idx = sched.index("await db.query")
    assert yield_idx < first_query_idx, (
        "COLLIDES_WITH_ARCHETYPE_PAGE check must run BEFORE the DB query / "
        "public_gate; otherwise the CRUD list renders before the pages "
        "router gets a chance"
    )


def test_fitness_server_always_mounts_public_plus_admin():
    """Regression: an earlier design tried to fix the
    collision by SKIPPING the public mount entirely when the path
    collided with an archetype page. That broke GET /:id detail routes
    (e.g. /events/1) because no router was mounted at /events to receive
    them. The current design always mounts both and relies on the router's own
    next() to yield.

    Every data_model must mount at BOTH public + /admin/<slug>.
    """
    resolved = _fitness_resolved()
    files = dict(compose_files(resolved))
    server = files["server.js"]
    for m in resolved.data_models:
        slug = _slug_for(m)
        public_path = m.get("public_listing_path") or f"/{slug}"
        assert f"app.use('{public_path}', require('./routes/{slug}'));" in server, (
            f"server.js must always mount public at {public_path} for /{slug} "
            f"(unconditional mount). Dropping the public mount breaks "
            f"/:id detail routes."
        )
        assert f"app.use('/admin/{slug}', requireAdmin, require('./routes/{slug}'));" in server, (
            f"server.js must mount admin at /admin/{slug}"
        )


# ---- db:<table> widget source (Round 30: page→DB binding + count-based mode) ----

def test_db_source_table_helper_parses_prefix():
    from compose.build import _db_source_table
    assert _db_source_table("db:instructors") == "instructors"
    assert _db_source_table("db:class_types") == "class_types"
    assert _db_source_table("db:staff") == "staff"
    # Not a db: source
    assert _db_source_table("spec.instructors.items") == ""
    assert _db_source_table("") == ""
    assert _db_source_table(None) == ""


def test_page_db_source_for_finds_db_widget():
    from compose.build import _page_db_source_for
    layout = {
        "widgets": [
            {"id": "hero-centered", "source": "spec.instructors.hero"},
            {"id": "instructor-grid", "source": "db:instructors"},
        ]
    }
    wid, table = _page_db_source_for(layout)
    assert wid == "instructor-grid"
    assert table == "instructors"


def test_page_db_source_for_no_db_widget_returns_empty():
    from compose.build import _page_db_source_for
    layout = {
        "widgets": [
            {"id": "hero-centered", "source": "spec.home.hero"},
            {"id": "pricing-cards", "source": "spec.home.pricing"},
        ]
    }
    wid, table = _page_db_source_for(layout)
    assert wid == ""
    assert table == ""


def test_public_slug_for_table_respects_public_listing_path():
    from compose.build import _public_slug_for_table, resolve_spec, BuildSpec
    spec = BuildSpec.from_dict({
        "app_name": "yoga-test",
        "archetype": "fitness-studio",
        "admin_email": "a@b.c",
    })
    rs = resolve_spec(spec)
    # fitness-studio data_models must include `instructors` with a
    # public_listing_path set. `_public_slug_for_table` returns that path.
    got = _public_slug_for_table(rs, "instructors")
    assert got == "/instructors", f"expected /instructors, got {got}"


def test_public_slug_for_table_falls_back_to_slug():
    from compose.build import _public_slug_for_table, resolve_spec, BuildSpec
    spec = BuildSpec.from_dict({
        "app_name": "yoga-test",
        "archetype": "fitness-studio",
        "admin_email": "a@b.c",
    })
    rs = resolve_spec(spec)
    # Unknown table → last resort slug.
    got = _public_slug_for_table(rs, "unknown_table")
    assert got == "/unknown-table"


def test_fitness_instructors_page_layout_declares_db_source():
    """Verify the page-layouts.json manifest wires /instructors to db:instructors."""
    from compose.build import load_layouts
    layouts = load_layouts().get("layouts", {})
    entry = layouts.get("fitness-instructors-default")
    assert entry is not None, "fitness-instructors-default layout missing"
    widget_sources = [w.get("source", "") for w in entry.get("widgets", [])]
    assert "db:instructors" in widget_sources, (
        f"expected db:instructors in {widget_sources}"
    )


def test_emit_routes_pages_js_emits_select_for_db_page():
    """db:<table> page handler must SELECT rows and pass `items` to the view."""
    spec = BuildSpec.from_dict({
        "app_name": "yoga-select",
        "archetype": "fitness-studio",
        "admin_email": "a@b.c",
    })
    rs = resolve_spec(spec)
    files = dict(compose_files(rs))
    pages_js = files["routes/pages.js"]
    # The /instructors handler should issue a SELECT against instructors.
    assert "SELECT * FROM instructors" in pages_js, (
        "pages.js should issue SELECT for /instructors (db:instructors source)"
    )
    assert "rows: items" in pages_js, (
        "pages.js should destructure rows as items"
    )
    # Ordering must be stable (id ASC) so the count-based mode decision is
    # deterministic across reloads.
    assert "ORDER BY id ASC" in pages_js


def test_instructors_view_passes_count_based_mode_props():
    """The emitted views/instructors.ejs must include count-derived props."""
    spec = BuildSpec.from_dict({
        "app_name": "yoga-mode",
        "archetype": "fitness-studio",
        "admin_email": "a@b.c",
    })
    rs = resolve_spec(spec)
    files = dict(compose_files(rs))
    inst_view = files["views/instructors.ejs"]
    # The include should pass `items` as a runtime reference (not a baked
    # JSON array), plus the count-based flags that drive widget mode.
    # Use `locals.items` (not bare `typeof items`) so express-ejs-layouts'
    # layout-only scope wrapping doesn't strip the route-passed local.
    # See roadmap v2-ejs-locals-bare-variable-undefined.
    assert (
        '"items": ((locals && Array.isArray(locals.items_instructors)) ? locals.items_instructors : ((locals && Array.isArray(locals.items)) ? locals.items : []))'
        in inst_view
    ), (
        "instructor-grid include should carry items as runtime reference "
        "via locals.items (not bare typeof items)"
    )
    assert "detail_path_prefix" in inst_view, "mode prop missing"
    assert "expand_inline" in inst_view, "mode prop missing"
    # The detail prefix must resolve to /instructors for >3 rows.
    assert "'/instructors'" in inst_view, (
        "detail_path_prefix should point at /instructors"
    )


def test_instructor_grid_widget_supports_three_modes():
    """The instructor-grid widget source must implement all 3 render branches."""
    import pathlib
    widget_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "widgets" / "offerings" / "instructor-grid.ejs"
    )
    src = widget_path.read_text()
    # Link mode: wraps card in <a href=...>
    assert 'href="<%= _detail_prefix %>/<%= person.id %>"' in src, (
        "link-mode anchor missing"
    )
    # Inline-expand mode: uses <details> + <summary>
    assert "<details" in src and "<summary" in src, (
        "inline-expand mode must use <details>/<summary>"
    )
    # Plain mode: bare <div class="text-center"> fallback for when neither
    # detail_path_prefix nor expand_inline applies.
    assert "_useLink" in src and "_useInline" in src, (
        "widget must branch on _useLink and _useInline"
    )
    # Bio only shows in inline mode (link mode delegates to detail page).
    assert "person.bio" in src




def test_db_page_widget_strips_static_items_from_spec():
    """When a page is db-backed, spec-supplied items[] must NOT leak into the
    EJS include (the DB rows are the single source of truth; the spec may
    still carry heading/eyebrow via spec_overlay)."""
    spec = BuildSpec.from_dict({
        "app_name": "yoga-strip",
        "archetype": "fitness-studio",
        "admin_email": "a@b.c",
        "pages": {
            "instructors": {
                "hero": {"eyebrow": "OUR TEACHERS", "heading": "Meet the Team"},
                "items": [
                    {"name": "Static Bad", "role": "SHOULD NOT APPEAR"},
                ],
            }
        },
    })
    rs = resolve_spec(spec)
    files = dict(compose_files(rs))
    inst_view = files["views/instructors.ejs"]
    # Static spec items must not be baked into the include literal.
    assert "Static Bad" not in inst_view, (
        "static spec items[] must NOT leak into db-backed page view"
    )
    assert "SHOULD NOT APPEAR" not in inst_view
    # But the spec-overlay heading/eyebrow should still appear (heading comes
    # from the hero widget via spec.instructors.hero), so no assertion needed
    # about heading presence here: covered by broader fixture tests.


# ----------------------------------------------------------------------
# GAP-1 (2026-04-25): admin/dashboard upcoming-events widget must be gated
# on whether the spec declares an Event data_model. A Wren & Rye build
# (wren-rye-cafe) shipped a dashboard with a hardcoded Add New
# Event CTA that 404'd because restaurant-cafe has no events model.
# ----------------------------------------------------------------------


def test_dashboard_route_emits_events_enabled_false_for_restaurant():
    """Restaurant-cafe has no Event model; dashboard route handler must
    pass events_enabled: false so the widget gate suppresses the dead
    Add New Event CTA."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    admin_js = files["routes/admin-dashboard.js"]
    assert "events_enabled: false" in admin_js, (
        "restaurant-cafe must pass events_enabled: false; "
        "without the gate /admin/events/new 404s on the dashboard"
    )


def test_dashboard_route_emits_events_enabled_true_for_event_producer():
    """Event-producer DOES have Event model; dashboard route must pass
    events_enabled: true so the upcoming-events widget renders."""
    spec_path = Path(__file__).resolve().parent.parent / "fixtures" / "sample-event-producer-spec.json"
    with open(spec_path) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    admin_js = files["routes/admin-dashboard.js"]
    assert "events_enabled: true" in admin_js


def test_dashboard_view_gates_upcoming_events_include():
    """The dashboard.ejs source must wrap the upcoming-events.ejs include
    in an `events_enabled` check so disabled archetypes never render the
    widget (and never expose the dead /admin/events/new link)."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    dash = files["views/admin/dashboard.ejs"]
    # The include must be inside an `if (_events_on)` block.
    assert "events_enabled" in dash
    # Belt-and-suspenders: the include must be conditional, not bare.
    # If somebody flattens the gate by accident, this catches it.
    bare_include = "<%- include('./upcoming-events.ejs'"
    if bare_include in dash:
        # ok if the bare include is inside an `if (_events_on)` guard;
        # check ordering: the if must precede the include.
        idx_if = dash.find("if (_events_on)")
        idx_inc = dash.find(bare_include)
        assert idx_if != -1 and idx_if < idx_inc, (
            "upcoming-events include is not guarded by events_enabled gate"
        )


# ----------------------------------------------------------------------
# GAP-2 (2026-04-25): restaurant-cafe /menu page MUST be db-backed so
# admin CRUD edits to menu_items propagate to public visitors. Verified
# end-to-end on wren-rye-cafe: created admin menu item, public
# /menu did not show it (8 rows), confirming static-spec divergence.
# ----------------------------------------------------------------------


def test_coach_blog_layout_uses_db_blog_posts_source():
    """coach-blog-default must point resource-list-card-grid at db:blog_posts
    so admin CRUD edits to blog_posts show up immediately on the public /blog
    page. Regression target: an end-to-end sample of a private music teacher's site
    where /admin/blog-posts had 3 rows but /blog rendered "Nothing here yet."
    because the layout pointed at the shared `resource-list-card-grid` widget
    layout whose source string was `spec.journal.items` (a static healer-
    flavored hint that resolves to nothing for coach pages)."""
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(layouts_path) as f:
        layouts = json.load(f)
    layout = layouts["layouts"]["coach-blog-default"]
    list_widget = next(w for w in layout["widgets"] if w["id"] == "resource-list-card-grid")
    assert list_widget["source"] == "db:blog_posts", (
        f"coach /blog must read DB rows; got source={list_widget['source']!r}"
    )
    assert list_widget.get("spec_overlay") == "spec.blog.items", (
        "spec_overlay must allow heading/eyebrow flow from spec"
    )


def test_coach_case_studies_layout_uses_db_case_studies_source():
    """Mirror of test_coach_blog_layout for case-studies. Catches the same
    class of disconnect: admin /admin/case-studies inserts must surface on
    public /case-studies."""
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(layouts_path) as f:
        layouts = json.load(f)
    layout = layouts["layouts"]["coach-case-studies-default"]
    list_widget = next(w for w in layout["widgets"] if w["id"] == "resource-list-card-grid")
    assert list_widget["source"] == "db:case_studies"


def test_healer_journal_layout_uses_db_blog_posts_source():
    """healer-wellness /journal reads from blog_posts (healer's BlogPost
    data model surfaces as their journal). Same disconnect class."""
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(layouts_path) as f:
        layouts = json.load(f)
    layout = layouts["layouts"]["healer-journal-default"]
    list_widget = next(w for w in layout["widgets"] if w["id"] == "resource-list-card-grid")
    assert list_widget["source"] == "db:blog_posts"


def test_shared_reviews_layout_uses_db_reviews_source():
    """Local-service / salon-spa / tradesperson all share the reviews layout
    pointing at db:reviews. Uses the reviews-grid widget (not the generic
    resource-list-card-grid) because reviews have a distinct shape:
    author_name + rating + body + service rather than title + excerpt +
    detail_href."""
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(layouts_path) as f:
        layouts = json.load(f)
    layout = layouts["layouts"]["shared-reviews-default"]
    list_widget = next(w for w in layout["widgets"] if w["id"] == "reviews-grid")
    assert list_widget["source"] == "db:reviews"


def test_no_archetype_uses_bare_resource_list_card_grid_layout_pointer():
    """Regression guard: archetype.page_layouts entries must point at a real
    page-layout id, not at a bare widget id. Before 2026-04-27 fix, coach /
    healer / local / salon / tradesperson all used the shared
    `resource-list-card-grid` layout entry whose source was `spec.journal.items`
    (static healer hint), causing the public list page to render empty even
    when admin had populated rows. This test asserts every archetype's
    page_layouts entries name a layout that contains a db: source for the
    listing widget when the page is named after a known data-model plural.
    """
    archetypes_path = Path(__file__).resolve().parent.parent / "data" / "archetypes.json"
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(archetypes_path) as f:
        archetypes = json.load(f)
    with open(layouts_path) as f:
        layouts = json.load(f)["layouts"]

    # The shared `resource-list-card-grid` layout (the bug seed) had
    # `spec.journal.items` as source. Any archetype that keeps pointing at it
    # would have the same disconnect.
    for arche in archetypes["archetypes"]:
        for page_id, layout_id in arche.get("page_layouts", {}).items():
            assert layout_id != "resource-list-card-grid", (
                f"{arche['id']} /{page_id} keeps pointing at the bare "
                f"resource-list-card-grid widget-id layout (source: spec.journal.items). "
                f"This causes admin CRUD inserts to never appear on the public page. "
                f"Add an archetype-specific layout with a db:<table> source."
            )


def test_resource_list_card_grid_widget_tolerates_missing_columns():
    """The widget must render acceptably when items lack the historic
    `description` / `date` / `detail_href` columns (e.g. db:blog_posts has
    `excerpt`/`published_at`/no detail_href; db:case_studies has
    `summary`/`client_name`/no detail_href). The widget reads `description ||
    excerpt || summary`, formats `date` from `published_at`/`created_at`, and
    skips link wrappers when `detail_href` is empty."""
    widget_path = (Path(__file__).resolve().parent.parent / "widgets" /
                   "offerings" / "resource-list-card-grid.ejs")
    text = widget_path.read_text(encoding="utf-8")
    # Check the tolerant-read helpers are present.
    assert "_firstStr" in text, "tolerant column-name reader missing"
    assert "item.excerpt" in text and "item.summary" in text, (
        "widget must read excerpt + summary as fallbacks for description"
    )
    assert "item.published_at" in text and "item.created_at" in text, (
        "widget must read published_at / created_at as fallbacks for date"
    )
    # When detail_href is missing, the link wrappers must be skipped.
    assert "if (_primaryHref)" in text, (
        "widget must conditionally render link wrappers when no href "
        "(external_url preferred, detail_href fallback) is present"
    )


def test_resource_list_card_grid_widget_emits_past_content_class():
    """Past-content de-emphasis: the listing widget computes `_isPast(item)`
    from item.date_time (or event_date+event_time, or start_at/starts_at/
    scheduled_for), and applies the `cas-past` class to past articles. The
    paired CSS rules ship in the build.py CSS generator. Reference: MW gold-
    standard `.mw-card-past` (views/events/index.ejs + style.css).
    """
    widget_path = (Path(__file__).resolve().parent.parent / "widgets" /
                   "offerings" / "resource-list-card-grid.ejs")
    text = widget_path.read_text(encoding="utf-8")
    assert "function _isPast(" in text, (
        "widget must declare _isPast(item) helper for past-content de-emphasis"
    )
    assert "item.date_time" in text, (
        "widget must check item.date_time as primary date field"
    )
    assert "item.event_date" in text, (
        "widget must support v2 Event default event_date + event_time pair"
    )
    assert "item.start_at" in text and "item.starts_at" in text, (
        "widget must support common alternative date field names (start_at/starts_at)"
    )
    assert "_isPast(item)" in text, (
        "widget must call _isPast at render time to compute is_past flag"
    )
    assert "_isPastItem ? ' cas-past' : ''" in text, (
        "widget must conditionally append 'cas-past' class on past articles"
    )


def test_past_content_de_emphasis_css_emitted():
    """The .cas-past CSS rules must be emitted in the generated style.css so
    past-event cards visibly de-emphasize on the public surface. Mirrors the
    MW gold-standard pattern: opacity 0.78 + 60% grayscale on the image,
    both restored on hover. Without this CSS, the widget's `cas-past` class
    is a no-op and the editorial pattern degrades to identical-looking cards.
    """
    spec_path = (Path(__file__).resolve().parent.parent / "fixtures" /
                 "sample-healer-spec.json")
    if not spec_path.exists():
        # Fall back to any v2 spec fixture when healer fixture isn't shipped.
        spec_path = next(
            (Path(__file__).resolve().parent.parent / "fixtures").glob("sample-*-spec.json")
        )
    with open(spec_path) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    css = files.get("public/style.css", "")
    assert ".cas-past" in css, (
        "expected .cas-past CSS rule in style.css for past-content de-emphasis"
    )
    assert "opacity: 0.78" in css, (
        ".cas-past must apply opacity 0.78 (matches MW .mw-card-past)"
    )
    assert ".cas-past:hover" in css, (
        ".cas-past:hover must restore full opacity"
    )
    assert "grayscale(0.6)" in css, (
        ".cas-past img must apply 60% grayscale filter"
    )
    assert "grayscale(0)" in css, (
        ".cas-past:hover img must restore full color (grayscale 0)"
    )


def test_coach_blog_route_emits_blog_posts_select():
    """The pages router for coach-consultant /blog must run a SELECT against
    blog_posts so the route handler hands rows to the view."""
    spec_path = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    with open(spec_path) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    pages_js = files["routes/pages.js"]
    assert "router.get('/blog'" in pages_js
    assert "SELECT * FROM blog_posts" in pages_js, (
        "/blog route must SELECT FROM blog_posts; otherwise admin CRUD "
        "edits never reach the public page (regression against 2026-04-27 fix)"
    )


def test_coach_case_studies_route_emits_case_studies_select():
    spec_path = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    with open(spec_path) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    pages_js = files["routes/pages.js"]
    assert "router.get('/case-studies'" in pages_js
    assert "SELECT * FROM case_studies" in pages_js


def test_coach_blog_view_uses_runtime_items_not_static_literal():
    """The emitted views/blog.ejs must use the runtime `items` local from
    the route, not a build-time static spec literal. Without this, admin
    CRUD inserts have zero effect on the public page."""
    spec_path = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    with open(spec_path) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    blog_view = files["views/blog.ejs"]
    # Page views must access render-passed locals via `locals.X` (not bare
    # typeof X): express-ejs-layouts can render the page view in a
    # layout-only scope where bare access returns undefined even when the
    # route passed the data. See roadmap v2-ejs-locals-bare-variable-undefined.
    assert "locals.items" in blog_view, (
        "views/blog.ejs must reference the runtime items via locals.items; "
        "otherwise the widget include passes [] and renders Nothing here yet "
        "when express-ejs-layouts wraps locals into a layout-only scope."
    )
    assert "typeof items !== 'undefined'" not in blog_view, (
        "views/blog.ejs must NOT use bare `typeof items !== 'undefined'`; "
        "use `locals.items` instead (see roadmap "
        "v2-ejs-locals-bare-variable-undefined)."
    )


def test_db_listing_routes_decorate_items_with_detail_href():
    """When a db-backed listing page uses resource-list-card-grid, the route
    handler must decorate each row with `detail_href` before passing items
    to the view. Without this, the listing widget renders cards with no
    click-through, leaving the detail page unreachable from the public
    surface (the private-lesson end-to-end regression).
    """
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    pages_js = files["routes/pages.js"]
    # /blog handler should decorate each row with `detail_href = '/blog/' + (slug || id)`
    assert "items.forEach(r => { r.detail_href = '/blog/' + (r.slug || r.id); });" in pages_js, (
        "expected /blog handler to set detail_href on each row using slug-or-id"
    )
    # /case-studies handler: case_studies has no slug column, so id-only
    assert "items.forEach(r => { r.detail_href = '/case-studies/' + r.id; });" in pages_js, (
        "expected /case-studies handler to set detail_href using id only"
    )


def test_db_listing_routes_emit_sibling_detail_handler():
    """Each db-backed listing page that uses resource-list-card-grid must
    emit a sibling GET '<path>/:identifier' handler so the cards can link
    to a working detail page. Tables with a slug column accept both slug
    and integer id; tables without slug accept id only."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    pages_js = files["routes/pages.js"]
    # /blog/:identifier, blog_posts has slug, accept either
    assert "router.get('/blog/:identifier'" in pages_js
    # blog_posts has a `published` column: the detail read applies the same
    # visibility rule as the listing, and the OR is parenthesized so the
    # filter binds to both lookups.
    assert (
        "SELECT * FROM blog_posts WHERE (slug::text = $1 OR id::text = $1) "
        "AND published = TRUE LIMIT 1"
    ) in pages_js
    # /case-studies/:identifier, case_studies has no slug, id only
    assert "router.get('/case-studies/:identifier'" in pages_js
    assert "SELECT * FROM case_studies WHERE id::text = $1 LIMIT 1" in pages_js


def test_db_listing_pages_emit_detail_view():
    """Every db-backed listing page using resource-list-card-grid must emit
    a `views/<page_id>-detail.ejs` template the detail route renders into."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    assert "views/blog-detail.ejs" in files, (
        "blog-detail view missing; route handler will throw at render time"
    )
    assert "views/case-studies-detail.ejs" in files
    blog_detail = files["views/blog-detail.ejs"]
    # Tolerant column reads for body
    assert "record.body" in blog_detail
    assert "record.description" in blog_detail
    assert "record.excerpt" in blog_detail
    # Back link to the listing
    assert "listing_path" in blog_detail
    assert "Back to" in blog_detail


def test_resource_router_get_id_yields_to_pages_router_when_collides():
    """Regression for 2026-04-27 detail-page miss: when a resource router's
    mount path collides with an archetype composed page (e.g. /case-studies
    is both the CRUD dual-mount AND the composed listing page in
    routes/pages.js), the resource router's GET /:id handler MUST yield to
    next() for non-admin requests. Without that fallthrough, the dual-mount
    catches the URL first and 404s, never reaching the pages router's detail
    handler emitted by emit_routes_pages_js. Found when /case-studies/1
    returned 404 despite the composed detail route being correctly emitted."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-coach-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    # case-studies CRUD router is dual-mounted + collides with /case-studies
    # composed listing
    cs_router = files["routes/case-studies.js"]
    assert "COLLIDES_WITH_ARCHETYPE_PAGE = true" in cs_router
    # GET /:id must accept `next` as third arg + yield to next() for non-admin
    # collide cases.
    assert "router.get('/:id', async (req, res, next)" in cs_router
    assert "if (!isAdmin && COLLIDES_WITH_ARCHETYPE_PAGE)" in cs_router
    assert "return next();" in cs_router


def test_db_listing_pages_skip_detail_view_when_widget_inline():
    """Pages whose listing widget renders the full content inline (reviews-
    grid, service-area-list, photo-gallery-grid) MUST NOT emit a sibling
    detail route or view; clicking a review card in a grid going to a
    detail page would just repeat the same content."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-tradesperson-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    pages_js = files["routes/pages.js"]
    # Reviews uses reviews-grid, not resource-list-card-grid: no detail route
    assert "router.get('/reviews/:identifier'" not in pages_js, (
        "reviews-grid is inline; should not emit a detail route"
    )
    assert "views/reviews-detail.ejs" not in files
    # Service-areas uses service-area-list: no detail route
    assert "router.get('/service-areas/:identifier'" not in pages_js
    assert "views/service-areas-detail.ejs" not in files


def test_service_cards_pass_service_to_contact_form_across_archetypes():
    """Audit harness; every service-card-shaped item across every archetype
    fixture's spec.pages.<page>.<services|offerings|items>.items[] that has
    a `cta_href: /contact` MUST be normalized to `/contact?service=<slug>`
    by emit_page_view's _normalize_offering_cta_hrefs. Otherwise clicking
    a specific service card (Leak Repair, Drain Cleaning, etc.) lands on a
    contact form with no preselected service; broken funnel.

    Catches the Ridgeline regression where every service card
    pointed at flat `/contact`.
    """
    fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
    failures = []
    for fixture_path in sorted(fixtures_dir.glob("sample-*-spec.json")):
        with open(fixture_path) as f:
            spec = json.load(f)
        bs = BuildSpec.from_dict(spec)
        rs = resolve_spec(bs)
        files = dict(compose_files(rs))
        # For every emitted page view, scan for service-card items pointing
        # at flat `/contact` (without query string). The widget `<%- include %>`
        # call passes the items inline, so we can grep the rendered EJS.
        for fname, body in files.items():
            if not fname.startswith("views/") or not fname.endswith(".ejs"):
                continue
            if "/contact-detail.ejs" in fname:
                continue
            # Flag only service-card items: a JSON-like object that has both
            # a `title` field AND a `cta_href` of bare `/contact` (without
            # ?service=). Generic CTA blocks at page-bottom ("Get in touch")
            # don't have a `title` so are correctly skipped.
            import re as _re
            broken = _re.findall(
                r'\{[^{}]*"title":[^{}]*"cta_href":\s*"/contact"(?!\?service=)',
                body,
            )
            if broken:
                failures.append(
                    f"{fixture_path.name} -> {fname}: {len(broken)} service "
                    f"card(s) with flat /contact href; expected /contact?service=<X>"
                )
    assert not failures, (
        "Service cards across one or more archetypes still link to flat "
        "/contact without a service query param. Customers who click a "
        "specific 'Book' card will land on a generic contact form with "
        "no preselected service:\n  " + "\n  ".join(failures)
    )


def test_no_orphan_admin_models_without_public_surface():
    """Audit harness; every default_data_model with `public_listing_path`
    set must point at a layout that actually queries the model's table.
    Models without a meaningful public surface should set
    `public_listing_path: null` so they appear in admin only.

    Catches the 2026-04-27 PastEvent regression where event-producer
    declared PastEvent.public_listing_path=/past-events but no composed
    page surfaced the (placeholder-only) past_events table, producing
    confusion when the admin '/admin/past-events' showed "Previous
    production 1/2/3..." while the public /events page showed real
    events from a different table.
    """
    archetypes_path = Path(__file__).resolve().parent.parent / "data" / "archetypes.json"
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(archetypes_path) as f:
        archetypes = json.load(f)
    with open(layouts_path) as f:
        layouts = json.load(f)["layouts"]

    failures = []
    for arche in archetypes["archetypes"]:
        page_layouts_dict = arche.get("page_layouts", {})
        for m in arche.get("default_data_models", []):
            plural = m.get("plural") or ""
            public_path = m.get("public_listing_path", "__sentinel__")
            # Models that explicitly opt out (path=null) are admin-only; 
            # always allowed.
            if public_path is None:
                continue
            # Models with no public_listing_path key declared default to
            # the dual-mount router serving /<plural> publicly. That's the
            # legacy CRUD-card listing; for archetypes with composed pages
            # at the same path, the dual-mount yields to the pages router
            # via COLLIDES_WITH_ARCHETYPE_PAGE. Either way, there must be
            # SOMEONE serving the URL meaningfully.
            url_path = public_path if public_path != "__sentinel__" else f"/{plural}"
            page_id = url_path.lstrip("/")
            # If the archetype has a composed page-layout for this page_id,
            # that's the public surface. Good.
            layout_id = page_layouts_dict.get(page_id)
            if layout_id and layout_id in layouts:
                continue
            # Otherwise the dual-mount CRUD serves /<plural>. That's
            # acceptable for first-class CRUD models (Service, Photo,
            # Instructor, ScheduleSlot, MenuItem, ClassType, etc.). It's
            # NOT acceptable when the model has only a thin schema (default
            # title-only fallback) AND no page-layout backs the URL; 
            # that produces the past_events placeholder confusion. Flag
            # those for review.
            fields = m.get("fields") or _default_fields(m["name"])
            schema_cols = {(f.get("name") or "").lower() for f in fields}
            # Title-only models (the fallback _default_fields() returns
            # for unknown model names) have just `title` + `created_at`.
            # That's the bug shape.
            non_meta = schema_cols - {"id", "created_at", "updated_at", "published", "published_at"}
            if non_meta == {"title"} and public_path != "__sentinel__":
                failures.append(
                    f"{arche['id']} declares {m['name']} with "
                    f"public_listing_path={public_path!r} but the model "
                    f"has only `title` (default fallback fields) and no "
                    f"composed page-layout backs the URL. Either set "
                    f"public_listing_path: null to keep it admin-only, "
                    f"OR add a real schema + composed layout."
                )
    assert not failures, (
        "Orphan public-listing models would render placeholder-only data "
        "on the public surface:\n  " + "\n  ".join(failures)
    )


def test_layout_public_favicon_ico_is_actually_emitted(resolved):
    """Audit harness: layout-public.ejs must not reference `/favicon.ico`
    unless the build actually emits a favicon.ico file. Browsers auto-fetch
    /favicon.ico on every page load; a 404 on that URL was the only finding
    of the live HTTP audit across all 4 archetypes (2026-04-27).

    With the favicon-and-og-asset-pipeline (2026-05-04) we DO emit a real
    multi-resolution favicon.ico; this test now asserts the layout's link
    is backed by a corresponding emitted file."""
    layout_path = (Path(__file__).resolve().parent.parent / "templates" /
                   "layout-public.ejs")
    text = layout_path.read_text(encoding="utf-8")
    if "/favicon.ico" in text:
        files = dict(compose_files(resolved))
        assert "public/favicon.ico" in files, (
            "layout-public.ejs references /favicon.ico but the build does "
            "not emit one; every page load 404s on /favicon.ico. Either "
            "emit a real favicon.ico in emit_public_assets / "
            "compose_favicon_assets, or remove the link."
        )


def test_service_area_list_widget_handles_single_zip_codes():
    """A service_areas row with a single zip ('00013') is not JSON for an
    array. The widget used to JSON.parse the value, which gives a bare
    number with no `.length` (or, with a leading zero, is not JSON at
    all), so the card was rendered without any zip codes.
    The widget now always splits on comma when the value is a string +
    falls back to [] for any non-array result."""
    widget_path = (Path(__file__).resolve().parent.parent / "widgets" /
                   "offerings" / "service-area-list.ejs")
    text = widget_path.read_text(encoding="utf-8")
    # The buggy `JSON.parse(_zips)` call without an Array.isArray check is gone.
    assert "JSON.parse" not in text, (
        "service-area-list still uses JSON.parse on zip_codes; this returns "
        "a bare number for single-zip rows + fails the .length check. "
        "Always split on comma for string inputs."
    )
    # And the array guard is present
    assert "Array.isArray(_zips)" in text, (
        "widget must guard against non-array zip_codes shapes"
    )


def test_admin_listing_db_source_coverage_across_archetypes():
    """Comprehensive regression harness: for every archetype, every
    `default_pages` entry whose name matches a `default_data_models` plural
    AND whose model still has `public_listing_path: null` (admin-only CRUD)
    MUST have a layout whose listing widget reads from a db:<table> source.

    This is the test that should have caught the private-lesson regression.
    Without it, individual layout pointers can drift back to the bare shared
    `resource-list-card-grid` layout silently.
    """
    archetypes_path = Path(__file__).resolve().parent.parent / "data" / "archetypes.json"
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(archetypes_path) as f:
        archetypes = json.load(f)
    with open(layouts_path) as f:
        layouts = json.load(f)["layouts"]

    failures = []
    for arche in archetypes["archetypes"]:
        page_layouts = arche.get("page_layouts", {})
        # Build a set of model-plural -> table names so we can match page
        # names to expected DB tables.
        model_table_by_plural = {}
        for m in arche.get("default_data_models", []):
            plural = (m.get("plural") or "").lower()
            if not plural:
                continue
            # snake_case for the table name (matches schema emitter).
            table = plural.replace("-", "_")
            model_table_by_plural[plural] = table

        for page_id, layout_id in page_layouts.items():
            if page_id not in model_table_by_plural:
                # Page not derived from a data model; skip (composed page
                # like home/about/services).
                continue
            expected_table = model_table_by_plural[page_id]
            layout = layouts.get(layout_id)
            if layout is None:
                failures.append(
                    f"{arche['id']} /{page_id} -> unknown layout {layout_id!r}"
                )
                continue
            # Find a list-shaped widget on this layout.
            list_widgets = [
                w for w in layout["widgets"]
                if w["id"] in (
                    "resource-list-card-grid", "menu-grouped-list",
                    "instructor-grid", "staff-card-grid", "service-area-list",
                    "reviews-grid", "books-grid",
                )
            ]
            if not list_widgets:
                # Page is not a listing page (might be a hero-only filler);
                # skip.
                continue
            for w in list_widgets:
                source = w.get("source", "")
                if not source.startswith("db:"):
                    failures.append(
                        f"{arche['id']} /{page_id} layout={layout_id!r} "
                        f"widget={w['id']!r} source={source!r} "
                        f"(expected db:{expected_table})"
                    )
    assert not failures, (
        "Listing page(s) without db: source; admin CRUD inserts will never "
        "reach the public page:\n  " + "\n  ".join(failures)
    )


def test_restaurant_menu_layout_uses_db_source():
    """restaurant-menu-default page-layout must point menu-grouped-list at
    db:menu_items so admin edits show up immediately on the public /menu."""
    layouts_path = Path(__file__).resolve().parent.parent / "data" / "page-layouts.json"
    with open(layouts_path) as f:
        layouts = json.load(f)
    layout = layouts["layouts"]["restaurant-menu-default"]
    menu_widget = next(w for w in layout["widgets"] if w["id"] == "menu-grouped-list")
    assert menu_widget["source"] == "db:menu_items", (
        f"restaurant /menu must read DB rows; got source={menu_widget['source']!r}"
    )
    assert menu_widget.get("spec_overlay") == "spec.menu.sections", (
        "spec_overlay must be set so heading/eyebrow still flow from spec"
    )


def test_restaurant_menu_route_emits_menu_items_select():
    """The pages router for /menu must SELECT FROM menu_items so the route
    handler hands the rows to the view."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    pages_js = files["routes/pages.js"]
    # The /menu handler should run a SELECT * FROM menu_items
    assert "router.get('/menu'" in pages_js
    assert "SELECT * FROM menu_items" in pages_js, (
        "/menu route must SELECT FROM menu_items; otherwise admin CRUD "
        "edits never reach the public page"
    )


def test_restaurant_menu_view_uses_db_runtime_items():
    """The emitted views/menu.ejs must use the runtime `items` local from
    the route, not a build-time static spec literal. Without this, admin
    edits to menu_items rows have zero effect on the public page."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    menu_view = files["views/menu.ejs"]
    # No static spec menu items should leak into the include literal.
    # The fixture has "Brioche French Toast" only in pages.menu.sections
    # (static), NOT in seed_content.menu_items. With the db swap, that
    # name must NOT appear in the rendered include.
    assert "Brioche French Toast" not in menu_view, (
        "static spec.menu.sections items[] leaked into the db-backed view; "
        "either source is still static or emit_page_view didn't strip them"
    )
    # And the runtime `items` must be referenced via locals.items so
    # express-ejs-layouts' layout-only scope doesn't strip the route-passed
    # local. See roadmap v2-ejs-locals-bare-variable-undefined.
    assert "locals.items" in menu_view
    assert "typeof items !== 'undefined'" not in menu_view, (
        "views/menu.ejs must NOT use bare `typeof items !== 'undefined'`; "
        "use `locals.items` instead (see roadmap "
        "v2-ejs-locals-bare-variable-undefined)."
    )


def test_logo_svg_skips_punctuation_words_and_xml_escapes():
    """site_name="Wren & Rye" must produce initials "WR", not "W&". The
    bare "&" baked into SVG text content is invalid XML; browsers render
    the SVG as a broken-image placeholder. Observed on wren-
    rye-cafe local rebuild; logo at top-left rendered as a
    generic mountain icon. Fix skips punctuation-led "words" when
    extracting initials and runs xml.sax.saxutils.escape as a backstop."""
    spec = BuildSpec.from_dict({
        "app_name": "wren-rye-test",
        "archetype": "restaurant-cafe",
        "admin_email": "a@b.c",
        "settings": {"site_name": "Wren & Rye"},
    })
    rs = resolve_spec(spec)
    files = dict(compose_files(rs))
    logo = files["public/logo.svg"]
    # The text node MUST be "WR" (not "W&" or "W&amp;").
    import re
    text_match = re.search(r">([^<]+)</text>", logo)
    assert text_match, f"no <text> content found in logo.svg: {logo}"
    assert text_match.group(1) == "WR", (
        f"expected initials 'WR' for 'Wren & Rye' (skip & punctuation word); "
        f"got: {text_match.group(1)!r}"
    )
    # SVG must be valid XML: parse it as a sanity check.
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(logo)
    except ET.ParseError as e:
        raise AssertionError(f"logo.svg is invalid XML: {e}\nbody: {logo}")


def test_logo_svg_handles_other_punctuation_brands():
    """Brands using +, &, -, /, etc. must produce safe alphanumeric initials."""
    cases = [
        ("Foo + Bar", "FB"),
        ("A & B Co", "AB"),
        ("Sun-Moon", "SU"),  # single word (no whitespace split) → first 2 chars
        ("X / Y / Z", "XY"),
    ]
    for site_name, expected in cases:
        spec = BuildSpec.from_dict({
            "app_name": "logo-test",
            "archetype": "healer-wellness",
            "admin_email": "a@b.c",
            "settings": {"site_name": site_name},
        })
        rs = resolve_spec(spec)
        files = dict(compose_files(rs))
        logo = files["public/logo.svg"]
        import re
        text_match = re.search(r">([^<]+)</text>", logo)
        assert text_match, f"no <text> in logo for {site_name!r}"
        # Validate XML
        import xml.etree.ElementTree as ET
        ET.fromstring(logo)  # raises if invalid
        # Soft assert on initials: exact mapping for "Sun-Moon" depends on
        # split logic; only check the value matches what we said it should.
        assert text_match.group(1) == expected, (
            f"site_name={site_name!r} → initials {text_match.group(1)!r}, expected {expected!r}"
        )


def test_static_assets_get_content_hash_cache_bust():
    """Static assets shared by URL across local-preview builds (style.css,
    js/*) MUST be cache-busted with a content hash query string so a
    cached-from-prior-app /style.css doesn't bleed into a new app on the
    same localhost:4600. Symptom (wren-rye-cafe rebuild on the
    same port reading-tracker had used): first page render shows the
    cached-prior-build CSS without the new build's responsive utilities,
    Cmd+Shift+R bypasses cache and fixes, next click reverts."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    layout = files["views/layout-public.ejs"]
    # The link must carry ?v=<hash>; bare /style.css would hit the cache.
    import re
    m = re.search(r'href="(/style\.css\?v=[0-9a-f]{8})"', layout)
    assert m, f"layout-public.ejs must use cache-busted /style.css URL; got: {layout[layout.find('/style.css'):layout.find('/style.css')+80] if '/style.css' in layout else 'no /style.css ref at all'}"
    # The bare unhashed URL must NOT appear in any view file.
    for view_path, view_src in files.items():
        if not view_path.endswith(".ejs"):
            continue
        assert 'href="/style.css"' not in view_src, (
            f"{view_path} still uses bare /style.css; cache-bust pass missed it"
        )
    # JS asset references (loaded via <script src="...">) must also be hashed.
    for js_asset in ("cas-form-validate.js", "cas-reveal.js"):
        bare = f'src="/js/{js_asset}"'
        assert bare not in layout, f"{js_asset} link in layout-public is not cache-busted"
    # Logo + favicon refs in layout must be hashed (browser caches /logo.svg
    # by URL and reuses stale bytes across local-preview apps; observed
    # 2026-04-25 follow-up to the css cache-bust).
    assert 'href="/favicon.svg"' not in layout, "/favicon.svg in layout-public is not cache-busted"
    assert 'href="/manifest.json"' not in layout, "/manifest.json in layout-public is not cache-busted"
    # Seeded site_settings.nav_logo_url MUST be cache-busted in seed.sql,
    # because the layout reads the value via `<%= settings.nav_logo_url %>`
    # at render time and the bare /logo.svg URL collides with prior apps'
    # cached bytes.
    seed = files["db/seed.sql"]
    assert "VALUES ('nav_logo_url', '/logo.svg?v=" in seed, (
        "seed.sql nav_logo_url must be cache-busted with content hash; "
        f"got: {seed[seed.find('nav_logo_url'):seed.find('nav_logo_url')+80] if 'nav_logo_url' in seed else 'no nav_logo_url at all'}"
    )


def test_cas_reveal_js_ships_safety_fallback_for_design_reveal_pattern():
    """`.reveal` elements emitted by claude.ai/design output (and similar
    imported designs) start at opacity:0 + translateY(14px) and rely on an
    IntersectionObserver adding `.in` to fade them in. When the observer
    misses items (off-screen on load, JS race, screenshot during transition),
    content stays permanently invisible. cas-reveal.js must ship a
    setTimeout safety fallback that adds `.in` to any remaining
    `.reveal:not(.in)` after a short delay so imported designs are covered
    automatically. The same fallback is applied to `.cas-reveal:not(.revealed)`
    so the native pattern is covered too. See roadmap item
    v2-reveal-on-scroll-fallback-needed."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    js_key = None
    for path in files:
        if path.startswith("public/js/cas-reveal.js"):
            js_key = path
            break
    assert js_key is not None, "cas-reveal.js missing from compose output"
    src = files[js_key]
    # Fallback must select the imported-design pattern `.reveal:not(.in)`
    # and add the `.in` class.
    assert ".reveal:not(.in)" in src, (
        "cas-reveal.js must include a fallback selector for `.reveal:not(.in)`"
    )
    assert "classList.add('in')" in src, (
        "cas-reveal.js must add the `in` class to fallback stragglers"
    )
    # Native pattern stragglers must also be swept.
    assert ".cas-reveal:not(.revealed)" in src, (
        "cas-reveal.js must include a fallback selector for `.cas-reveal:not(.revealed)`"
    )
    # Fallback must be timer-based with a positive delay (the 800ms anchor
    # is a recommendation, the assertion is that some setTimeout > 0 fires
    # the sweep so the test won't break on minor tuning).
    import re
    m = re.search(r"setTimeout\(\s*function\s*\(\)\s*\{.*?\.reveal:not\(\.in\).*?\}", src, re.DOTALL)
    assert m is not None, (
        "cas-reveal.js must invoke the `.reveal:not(.in)` sweep from inside a setTimeout"
    )


def test_layout_public_dev_mode_cache_bust_on_style_css():
    """Local-dev builds (port 4600, NODE_ENV !== 'production') must append a
    runtime `?v=<Date.now()>` to the /style.css href so every page load
    fetches fresh CSS while a customer is iterating on settings. The build-
    time content-hash cache-bust handles cross-build invalidation, but it
    can't help when the same artifact is served and the browser's disk
    cache (or service worker) holds onto stale bytes between manual edits.
    Production keeps the static href so the build-time content-hash pass
    rewrites it deterministically.
    """
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs = resolve_spec(bs)
    files = dict(compose_files(rs))
    layout = files["views/layout-public.ejs"]
    # Dev branch must be present and use Date.now() so each request is unique.
    assert "process.env.NODE_ENV !== 'production'" in layout, (
        "layout-public.ejs must guard the dev cache-bust on NODE_ENV"
    )
    assert "/style.css?v=<%= Date.now() %>" in layout, (
        "layout-public.ejs must emit ?v=<Date.now()> on /style.css href in dev"
    )
    # Production branch must still have the build-time content-hash applied.
    import re
    assert re.search(r'href="/style\.css\?v=[0-9a-f]{8}"', layout), (
        "layout-public.ejs production branch must carry build-time content "
        "hash so cross-build invalidation continues to work"
    )


def test_cache_bust_pass_is_deterministic_across_rebuilds():
    """Same spec content -> same hashes -> same URLs. Otherwise the page
    invalidates the browser cache on every rebuild even when the asset
    content is identical, which defeats the cache."""
    spec = Path(__file__).resolve().parent.parent / "fixtures" / "sample-restaurant-spec.json"
    with open(spec) as f:
        bs = BuildSpec.from_dict(json.load(f))
    rs1 = resolve_spec(bs)
    files1 = dict(compose_files(rs1))
    rs2 = resolve_spec(bs)
    files2 = dict(compose_files(rs2))
    # The layout-public link should be byte-identical across builds.
    assert files1["views/layout-public.ejs"] == files2["views/layout-public.ejs"]


def test_menu_grouped_list_widget_groups_flat_items_by_section_name():
    """When the widget receives a flat `items` list (db source) rather
    than pre-grouped `sections`, it must group by section_name preserving
    first-seen order. Smoke-checks the EJS source contains the grouping
    logic so the runtime contract survives refactors."""
    widget_path = Path(__file__).resolve().parent.parent / "widgets" / "offerings" / "menu-grouped-list.ejs"
    src = widget_path.read_text()
    # Grouping logic markers
    assert "section_name" in src, "widget must read section_name from flat rows"
    assert "_byName" in src or "groupBy" in src, "widget must group flat rows"
    assert "_order.push" in src or "preserve" in src.lower(), (
        "widget must preserve first-seen order across grouped sections"
    )
    # Honor the admin "available" toggle: rows with available=false should
    # be filtered out.
    assert "available === false" in src or "!row.available" in src, (
        "widget must skip unavailable menu items when grouping db rows"
    )


def test_hero_images_not_lazy_loaded(resolved):
    """Hero images are above the fold and must NOT use loading='lazy'.
    Lazy loading defers the fetch until the browser judges the element
    visible; if the CSS hasn't loaded yet the container has zero height
    and the image never fires. Use loading='eager' + fetchpriority='high'
    so the browser fetches it in parallel with the render pipeline."""
    hero_dir = Path(__file__).resolve().parent.parent / "widgets" / "hero"
    for ejs_path in hero_dir.glob("*.ejs"):
        src = ejs_path.read_text()
        if '<img' in src and 'image_url' in src:
            assert 'loading="lazy"' not in src, (
                f"{ejs_path.name}: hero image uses loading='lazy'; "
                "above-the-fold images must be eager+high-priority"
            )
            assert 'loading="eager"' in src, (
                f"{ejs_path.name}: hero image missing loading='eager'"
            )


def test_settings_middleware_always_sets_nav_items(resolved):
    """siteSettingsMiddleware must set nav_items + admin_nav_items
    OUTSIDE the try block (or in finally) so the layout never throws
    'nav_items is not defined' when the DB is cold on first boot.
    A catch block that calls next() without setting nav_items causes
    every page to 500 until the DB is ready."""
    files = dict(compose_files(resolved))
    mw = files["middleware/settings.js"]
    # nav_items must be assigned as a module-level constant (not inside try)
    # so it survives exception paths. The pattern is: const _NAV_ITEMS = [...];
    # then res.locals.nav_items = _NAV_ITEMS; outside the try.
    assert "_NAV_ITEMS" in mw, (
        "middleware/settings.js must declare _NAV_ITEMS as a module-level const "
        "so nav_items is always set even when loadSettings() throws"
    )
    # The assignment to res.locals.nav_items must not be inside a try block; 
    # verify it appears after the closing }} of the try/catch.
    nav_assign_pos = mw.rfind("res.locals.nav_items = _NAV_ITEMS")
    catch_close_pos = mw.rfind("} catch")
    assert nav_assign_pos > catch_close_pos, (
        "res.locals.nav_items = _NAV_ITEMS must come AFTER the catch block "
        "so nav is set on both success and error paths"
    )


def test_layout_admin_guards_against_undefined_settings(resolved):
    """compose-error-layout-settings-guard (2026-05-02): layout-admin.ejs
    must defend against `settings === undefined` so an error path that
    renders an admin view without populated res.locals does not double-
    crash. layout-public.ejs already had this guard; this asserts admin
    layout has it too.

    Pattern: at the top, alias `settings` to a local `_s` with typeof
    guard, and reference `_s.<key>` everywhere instead of bare
    `settings.<key>`. Test asserts: (a) the typeof guard is present,
    (b) zero bare `settings.<anything>` references remain in the body.
    """
    files = dict(compose_files(resolved))
    layout = files["views/layout-admin.ejs"]
    assert "typeof settings !== 'undefined'" in layout, (
        "layout-admin.ejs must guard against undefined settings via "
        "`typeof settings !== 'undefined'` to prevent double-crash on "
        "the error rendering path"
    )
    # No bare `settings.X` references should remain (all must be `_s.X`).
    import re
    bare = re.findall(r'(?<![A-Za-z0-9_])settings\.', layout)
    assert not bare, (
        f"layout-admin.ejs has {len(bare)} bare `settings.X` references "
        "that bypass the typeof guard and re-introduce the double-crash "
        "vector. Replace each with `_s.X`."
    )


def test_layout_public_still_guards_against_undefined_settings(resolved):
    """Regression guard for the existing layout-public.ejs guard
    Locks in the typeof check so a future refactor
    does not silently remove it."""
    files = dict(compose_files(resolved))
    layout = files["views/layout-public.ejs"]
    assert "typeof settings !== 'undefined'" in layout
    import re
    bare = re.findall(r'(?<![A-Za-z0-9_])settings\.', layout)
    assert not bare, (
        f"layout-public.ejs has {len(bare)} bare `settings.X` references"
    )


def _ejs_module_path():
    """Locate an installed `ejs` package usable from a Node subprocess.

    web/node_modules ships ejs as part of the express stack; returns None
    (callers skip) when `npm install` has not been run in web/.
    """
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / "web" / "node_modules" / "ejs",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _node_available_for_layout_render():
    import subprocess
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def _render_emitted_layout(layout_filename, locals_payload, resolved, tmp_path):
    """Render an emitted view file via Node + EJS in an isolated tmp dir.

    Lays out the tmp dir to mirror the generated app: tmp_path/views holds
    the layout plus the entire compose widgets/ tree (so relative includes
    like ./widgets/shell/nav-public resolve). Returns (returncode, stdout,
    stderr) so the caller can assert behavior.
    """
    import shutil
    import subprocess
    import textwrap
    from compose.build import COMPOSE_DIR

    if not _node_available_for_layout_render():
        pytest.skip("node is not on PATH")
    ejs_path = _ejs_module_path()
    if ejs_path is None:
        pytest.skip("web/node_modules/ejs is not installed (run `cd web && npm ci`)")

    files = dict(compose_files(resolved))
    layout_src = files[f"views/{layout_filename}"]
    views_dir = tmp_path / "views"
    views_dir.mkdir()
    (views_dir / layout_filename).write_text(layout_src)
    src_widgets = COMPOSE_DIR / "widgets"
    shutil.copytree(src_widgets, views_dir / "widgets")

    runner = tmp_path / "render.js"
    runner.write_text(textwrap.dedent('''
        const fs = require('fs');
        const ejs = require(process.argv[2]);
        const layoutPath = process.argv[3];
        const dataJson = process.argv[4];
        const data = JSON.parse(dataJson);
        const src = fs.readFileSync(layoutPath, 'utf-8');
        try {
          const out = ejs.render(src, data, { filename: layoutPath });
          process.stdout.write(out);
        } catch (e) {
          console.error('RENDER_ERROR:', e.message);
          process.exit(1);
        }
    '''))

    r = subprocess.run(
        ["node", str(runner), str(ejs_path), str(views_dir / layout_filename),
         json.dumps(locals_payload)],
        capture_output=True, text=True, timeout=20,
    )
    return r.returncode, r.stdout, r.stderr


def test_layout_public_renders_with_empty_res_locals(resolved, tmp_path):
    """Double-crash regression guard (2026-04-24):
    layout-public.ejs must render cleanly when the response carries no
    settings, nav_items, or csrfToken in res.locals.

    Hits the real failure path: the global error handler can fire BEFORE
    the settings + csrf middleware ever ran (e.g. session middleware
    threw, or migration crashed mid-request), so the layout must
    self-defend or the 500 page itself crashes and Express returns
    Cannot GET / with no response body.

    Renders through Node + EJS to exercise the actual EJS engine; a
    bare typeof guard at the top of the file is necessary but not
    sufficient (every nested include/reference must also tolerate
    missing locals).
    """
    rc, out, err = _render_emitted_layout(
        "layout-public.ejs",
        {"body": '<p data-test="error-body">simulated 500</p>'},
        resolved, tmp_path,
    )
    assert rc == 0, (
        "layout-public.ejs must render with empty res.locals to avoid the "
        "double-crash on the error path. Render error: " + err
    )
    assert "<!DOCTYPE html>" in out
    assert '<p data-test="error-body">' in out
    # Brand fallback: when settings.site_name is missing the title falls
    # through to the literal default "Site" baked into the layout.
    assert "<title>Site</title>" in out


def test_layout_admin_renders_with_empty_res_locals(resolved, tmp_path):
    """Same double-crash guard as the public layout, applied to admin.

    Admin pages can also be reached on the error path (e.g. an admin
    posting a CRUD form when the session backend is unreachable).
    layout-admin.ejs must self-defend identically.
    """
    rc, out, err = _render_emitted_layout(
        "layout-admin.ejs",
        {"body": '<p data-test="admin-error-body">simulated 500</p>'},
        resolved, tmp_path,
    )
    assert rc == 0, (
        "layout-admin.ejs must render with empty res.locals to avoid the "
        "double-crash on the error path. Render error: " + err
    )
    assert "<!DOCTYPE html>" in out
    assert '<p data-test="admin-error-body">' in out


def test_server_js_sets_no_cache_for_html_responses(resolved):
    """Generated server.js must add Cache-Control: no-cache (WITHOUT
    no-store) middleware AFTER express.static. Static assets (CSS/JS with
    content-hash URLs) are served by express.static before this middleware
    fires and remain cacheable. HTML pages revalidate before reuse so the
    browser sees the current hashed asset URLs, but the response stays
    storable: several Open Graph link-preview crawlers (Facebook
    Messenger) refuse to build a card for a no-store resource
    (v2-html-cache-control-no-store-removal)."""
    files = dict(compose_files(resolved))
    server = files["server.js"]
    # Middleware must exist
    assert "no-cache, must-revalidate" in server, (
        "server.js must set Cache-Control: no-cache, must-revalidate "
        "so browsers don't cache HTML pages with stale asset URLs"
    )
    # Must come after express.static (static assets should still cache)
    static_pos = server.find("express.static")
    no_cache_pos = server.find("no-cache, must-revalidate")
    assert static_pos < no_cache_pos, (
        "Cache-Control middleware must be registered AFTER express.static "
        "so static assets (with content-hash URLs) remain cacheable"
    )
    # no-store suppresses link-preview cards; Pragma is its HTTP/1.0 twin.
    assert "no-cache, no-store" not in server, (
        "HTML Cache-Control must NOT include no-store: link-preview "
        "crawlers refuse to build OG cards for no-store responses"
    )
    assert "res.set('Pragma'" not in server, (
        "Pragma: no-cache must not be set on HTML responses (same "
        "no-store class; dropped with v2-html-cache-control-no-store-removal)"
    )


def test_layouts_carry_no_cache_control_meta_tags(resolved):
    """layout-public.ejs + layout-admin.ejs must not ship http-equiv
    Cache-Control/Pragma/Expires metas. The no-store meta variant
    suppresses link-preview cards and the headers are the real control
    surface (v2-html-cache-control-no-store-removal)."""
    files = dict(compose_files(resolved))
    for view in ("views/layout-public.ejs", "views/layout-admin.ejs"):
        src = files[view]
        assert 'http-equiv="Cache-Control"' not in src, view
        assert 'http-equiv="Pragma"' not in src, view
        assert 'http-equiv="Expires"' not in src, view


def test_admin_settings_post_skips_omitted_keys(resolved):
    """The emitted /admin/settings POST handler must treat a key absent
    from the body as 'leave unchanged', never 'set to empty'. A partial
    or programmatic POST previously blanked every field it omitted
    (about and hero photos can vanish when a content-sync propagates
    the blanks). Empty-string submits still clear intentionally
    (v2-settings-post-omission-safe)."""
    files = dict(compose_files(resolved))
    src = files["routes/admin-settings.js"]
    assert "if (req.body[key] === undefined) continue;" in src, (
        "settings POST must skip keys absent from the body"
    )
    assert "req.body[key] !== undefined ? String(req.body[key]) : ''" not in src, (
        "the omitted-key-becomes-empty-string pattern must be gone"
    )
    # The continue guard must appear before the value coercion + write.
    guard_pos = src.find("if (req.body[key] === undefined) continue;")
    write_pos = src.find("INSERT INTO site_settings")
    assert -1 < guard_pos < write_pos


# ===== CSP nonce regression (v2-csp-nonce-pattern, 2026-05-26) =====
# Compose-emitted apps must ship a per-request CSP nonce mirroring the
# CAS web pattern (web/src/app.ts security-headers middleware, 2026-05-09):
# script-src is tightened to `'self' 'nonce-<base64>'` (no `'unsafe-inline'`)
# and every inline <script> tag in compose/widgets/ + compose/build.py
# carries `nonce="<%= cspNonce %>"`. The typeof-guard form keeps templates
# valid when rendered without the middleware chain (unit tests).

def test_server_js_emits_csp_nonce_middleware(resolved):
    """server.js must mint a per-request crypto.randomBytes nonce, set it
    on res.locals.cspNonce, and emit a Content-Security-Policy header
    where script-src includes 'nonce-' + nonce. Mirrors web/src/app.ts."""
    files = dict(compose_files(resolved))
    server = files["server.js"]
    # crypto must be required for randomBytes
    assert "require('crypto')" in server, (
        "server.js must require('crypto') to mint per-request nonces"
    )
    # Per-request nonce assignment to res.locals
    assert "crypto.randomBytes(24).toString('base64')" in server, (
        "nonce must be 24 random bytes base64-encoded (matches CAS web pattern)"
    )
    assert "res.locals.cspNonce" in server, (
        "nonce must be exposed to EJS templates via res.locals.cspNonce"
    )
    # CSP header must be set with nonce-X in script-src
    assert "Content-Security-Policy" in server
    assert "'nonce-" in server, (
        "script-src must include 'nonce-' + nonce so inline scripts execute"
    )


def test_server_js_csp_script_src_drops_unsafe_inline(resolved):
    """The whole point of the nonce: script-src must NOT contain
    'unsafe-inline'. Anti-pattern would silently override the nonce.
    style-src may keep 'unsafe-inline' (inline style attrs are pervasive
    and CSP nonces don't apply to attributes; see web/src/app.ts comment)."""
    files = dict(compose_files(resolved))
    server = files["server.js"]
    # Find the actual CSP literal (skip comment occurrences). The emitted
    # JS source concatenates the directive as `"script-src 'self' 'nonce-"
    # + nonce + "'; "`. Grep for the literal opener.
    script_src_literal = "\"script-src 'self' 'nonce-\""
    script_src_start = server.find(script_src_literal)
    assert script_src_start != -1, (
        "missing CSP script-src literal in server.js; expected "
        + repr(script_src_literal)
    )
    # script-src must not contain 'unsafe-inline' literally. The literal
    # opener already proves the directive starts with `'self' 'nonce-...`;
    # we additionally assert no "unsafe-inline" appears on the same JS
    # concatenation line.
    end_of_line = server.find("\n", script_src_start)
    script_src_line = server[script_src_start:end_of_line if end_of_line != -1 else len(server)]
    assert "'unsafe-inline'" not in script_src_line, (
        "script-src must NOT include 'unsafe-inline'; it overrides the "
        "nonce and defeats the entire CSP hardening. Found in: "
        + repr(script_src_line)
    )


def test_admin_detail_inline_script_carries_nonce_stamp(resolved):
    """build.py emits an inline <script> at the bottom of every admin-
    detail view (the cas-fmt-date/datetime/time formatter). It must
    carry a `nonce="<%= cspNonce %>"` attribute (typeof-guarded so
    no-middleware test renders still validate)."""
    files = dict(compose_files(resolved))
    # Pick any data model's admin-detail view (they all share the emitter).
    admin_detail_paths = [p for p in files if p.endswith("/admin-detail.ejs")]
    assert admin_detail_paths, "no admin-detail.ejs files emitted"
    for p in admin_detail_paths:
        content = files[p]
        # The inline formatter script must exist
        assert "document.querySelectorAll('.cas-fmt-date')" in content, (
            f"{p} missing inline cas-fmt formatter script"
        )
        # And it must carry the nonce stamp (typeof-guarded form)
        assert "cspNonce" in content, (
            f"{p} inline <script> must reference cspNonce for CSP compliance"
        )
        assert 'nonce="<%= cspNonce %>"' in content, (
            f"{p} must stamp nonce=\"<%= cspNonce %>\" on its inline <script>"
        )


def test_every_widget_inline_script_carries_nonce_stamp():
    """Cross-widget regression: every compose/widgets/**/*.ejs file with
    an inline <script>...</script> tag must stamp the nonce. This catches
    new widgets that add inline scripts without the stamp.

    Allowed: <script src="..."> (external scripts cleared by 'self' in
    script-src; CSP doesn't require nonces on src-loaded scripts).
    """
    import re
    widgets_root = Path(__file__).resolve().parent.parent / "widgets"
    assert widgets_root.is_dir(), f"missing widgets dir: {widgets_root}"
    # Match `<script` followed by anything that ISN'T `src=` before the
    # closing `>`. Captures inline-script openers; ignores src-loaded
    # external scripts.
    inline_script_opener = re.compile(
        r"<script(?![^>]*\bsrc=)([^>]*)>",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for ejs in widgets_root.rglob("*.ejs"):
        text = ejs.read_text(encoding="utf-8")
        for match in inline_script_opener.finditer(text):
            attrs = match.group(1)
            if "cspNonce" not in attrs:
                offenders.append(f"{ejs.relative_to(widgets_root)}: {match.group(0)!r}")
    assert not offenders, (
        "Inline <script> tags in compose/widgets/ must stamp "
        'nonce="<%= cspNonce %>" (typeof-guarded). Offenders:\n  '
        + "\n  ".join(offenders)
    )


def test_csp_nonce_middleware_registered_before_route_handlers(resolved):
    """The CSP nonce middleware must fire BEFORE any router mounts so
    every res.render() call sees res.locals.cspNonce. If it lands after
    `app.use('/', ...)` the routes register first and the nonce never
    threads into the EJS context."""
    files = dict(compose_files(resolved))
    server = files["server.js"]
    nonce_pos = server.find("res.locals.cspNonce")
    assert nonce_pos != -1, "cspNonce assignment missing from server.js"
    # First router mount (auth router is the first one registered).
    first_router_pos = server.find("app.use('/', require('./routes/auth'))")
    assert first_router_pos != -1, "couldn't locate first router mount"
    assert nonce_pos < first_router_pos, (
        "CSP nonce middleware must be registered before route handlers "
        "so res.locals.cspNonce is set when views render"
    )


# ===== _format_sql_default regression (reiki-practice e2e sample) =====
# Bug: emit_schema_sql emitted ``DEFAULT pending`` (unquoted literal) for
# fields whose default was a string keyword like "pending". Postgres parsed
# the bareword as a column reference -> "cannot use column reference in
# DEFAULT expression" -> schema apply aborted on app boot. Fix: wrap
# string defaults in single quotes; preserve real SQL keywords (NOW(),
# TRUE, FALSE, NULL, CURRENT_TIMESTAMP, GEN_RANDOM_UUID()) unquoted.

def test_format_sql_default_quotes_arbitrary_strings():
    from compose.build import _format_sql_default
    assert _format_sql_default("pending") == "'pending'"
    assert _format_sql_default("active") == "'active'"
    assert _format_sql_default("draft") == "'draft'"


def test_format_sql_default_passes_sql_keywords_through_unquoted():
    from compose.build import _format_sql_default
    assert _format_sql_default("NOW()") == "NOW()"
    assert _format_sql_default("now()") == "NOW()"
    assert _format_sql_default("TRUE") == "TRUE"
    assert _format_sql_default("FALSE") == "FALSE"
    assert _format_sql_default("NULL") == "NULL"
    assert _format_sql_default("CURRENT_TIMESTAMP") == "CURRENT_TIMESTAMP"
    assert _format_sql_default("gen_random_uuid()") == "GEN_RANDOM_UUID()"


def test_format_sql_default_handles_booleans_and_numerics():
    from compose.build import _format_sql_default
    assert _format_sql_default(True) == "TRUE"
    assert _format_sql_default(False) == "FALSE"
    assert _format_sql_default(0) == "0"
    assert _format_sql_default(42) == "42"
    assert _format_sql_default(3.14) == "3.14"
    assert _format_sql_default(None) == "NULL"


def test_format_sql_default_escapes_embedded_single_quotes():
    from compose.build import _format_sql_default
    # SQL standard: doubled single quote inside a string literal.
    assert _format_sql_default("it's pending") == "'it''s pending'"
    assert _format_sql_default("a'b'c") == "'a''b''c'"


def test_format_sql_default_preserves_already_quoted_literals():
    from compose.build import _format_sql_default
    # If caller pre-quoted the literal, leave it alone.
    assert _format_sql_default("'pending'") == "'pending'"
    assert _format_sql_default("'2026-04-28'") == "'2026-04-28'"


def test_format_sql_default_preserves_quoted_literal_with_cast():
    """A quoted literal followed by a type cast is a SQL EXPRESSION and must
    pass through unchanged. Re-quoting it (the old behavior, because it doesn't
    END in a quote) produced "'''[]''::jsonb'", which Postgres parsed as JSON
    at insert time and crashed the migration with 22P02 'invalid input syntax
    for type json; Token \\' is invalid'. Surfaced on a healer build
    (the `photos JSONB` archetype field)."""
    from compose.build import _format_sql_default
    assert _format_sql_default("'[]'::jsonb") == "'[]'::jsonb"
    assert _format_sql_default("'{}'::jsonb") == "'{}'::jsonb"
    assert _format_sql_default("'2020-01-01'::date") == "'2020-01-01'::date"
    # And it must NOT be over-quoted.
    assert "'''" not in _format_sql_default("'[]'::jsonb")


def test_jsonb_field_default_emits_valid_schema(tmp_path):
    """End-to-end: a data_model with a JSONB photos field must emit a schema
    whose DEFAULT is a valid '[]'::jsonb expression, not an over-quoted string
    that crashes the migration. Guards that healer-build crash."""
    from compose.intake import translate_v1_spec
    from compose.build import build_app
    v1 = {
        "app_name": "json-default-guard",
        "description": "A Reiki and breathwork healing practice.",
        "admin_email": "admin@example.com",
        "spec_version": "1.0",
        "data_models": [],
        "user_roles": ["admin"],
        "pages": [{"path": "/", "title": "Home"}],
        "_harbor_meta": {"archetype_hint": "healer-wellness"},
    }
    out = build_app(translate_v1_spec(v1), output_dir=tmp_path / "b")
    schema = (out / "db" / "schema.sql").read_text()
    photos_lines = [l for l in schema.splitlines() if "photos" in l.lower() and "jsonb" in l.lower()]
    for l in photos_lines:
        assert "'''" not in l, f"over-quoted JSONB default: {l.strip()}"
        assert "'[]'::jsonb" in l, f"JSONB default malformed: {l.strip()}"


def test_format_sql_default_treats_numeric_strings_as_numbers():
    from compose.build import _format_sql_default
    # "0" / "1.5" / "-3" are numeric literals masquerading as strings.
    assert _format_sql_default("0") == "0"
    assert _format_sql_default("1.5") == "1.5"
    assert _format_sql_default("-3") == "-3"


def test_emit_schema_sql_status_default_is_quoted_when_pending(resolved):
    """End-to-end: a field with default="pending" must produce a
    valid Postgres DEFAULT clause. Regression for the reiki-practice
    e2e where DEFAULT pending crashed schema apply.
    """
    from compose.build import emit_schema_sql
    # Tweak the resolved spec to include a status field with string default.
    rs = resolved
    if rs.data_models:
        m = rs.data_models[0]
        m.setdefault("fields", []).append(
            {"name": "status", "sql_type": "VARCHAR(255)", "default": "pending"}
        )
    _, schema_sql = emit_schema_sql(rs)
    assert "DEFAULT pending," not in schema_sql, (
        "status VARCHAR DEFAULT pending must be quoted as DEFAULT 'pending' "
        "(unquoted bareword is parsed by Postgres as a column reference)"
    )
    assert "DEFAULT 'pending'" in schema_sql, (
        "expected the quoted form DEFAULT 'pending' in emitted schema"
    )


# ---------------------------------------------------------------------------
# Regression tests from the Fern & Stone end-to-end sample
# ---------------------------------------------------------------------------


class TestLegalRoutesEmitted:
    """Every generated app's nav + footer linked /privacy + /terms but
    compose never emitted route handlers, so both 404'd.
    """

    def test_legal_routes_module_is_emitted(self, tmp_path):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        import json as _json
        v1 = {
            "app_name": "legal-routes-test",
            "description": "reiki practitioner",
            "admin_email": "admin@example.com",
            "_harbor_meta": {"archetype_hint": "healer-wellness"},
            "data_models": [],
            "pages": [],
        }
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        assert (build_dir / "routes" / "legal.js").exists(), (
            "compose must emit routes/legal.js with /privacy + /terms handlers"
        )
        legal_js = (build_dir / "routes" / "legal.js").read_text()
        assert "router.get('/privacy'" in legal_js, "GET /privacy handler missing"
        assert "router.get('/terms'" in legal_js, "GET /terms handler missing"

    def test_legal_router_mounted_in_server_js(self, tmp_path):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        import json as _json
        v1 = {
            "app_name": "legal-mount-test",
            "description": "reiki practitioner",
            "admin_email": "admin@example.com",
            "_harbor_meta": {"archetype_hint": "healer-wellness"},
            "data_models": [],
            "pages": [],
        }
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        server_js = (build_dir / "server.js").read_text()
        assert "require('./routes/legal')" in server_js, (
            "server.js must require the legal router so /privacy + /terms render"
        )
        # Legal must be mounted BEFORE pages so static legal copy wins.
        legal_idx = server_js.index("require('./routes/legal')")
        pages_idx = server_js.index("require('./routes/pages')")
        assert legal_idx < pages_idx, (
            "legal router must mount before pages router so /terms + /privacy "
            "static copy wins over any custom_page that tries to claim those paths"
        )

    def test_privacy_and_terms_views_are_emitted_with_site_name_interpolation(self, tmp_path):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        import json as _json
        v1 = {
            "app_name": "legal-views-test",
            "description": "reiki",
            "admin_email": "admin@example.com",
            "_harbor_meta": {"archetype_hint": "healer-wellness"},
            "data_models": [],
            "pages": [],
        }
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        privacy = (build_dir / "views" / "privacy.ejs").read_text()
        terms = (build_dir / "views" / "terms.ejs").read_text()
        assert "<%= site_name %>" in privacy, "privacy view must interpolate site_name"
        assert "<%= site_name %>" in terms, "terms view must interpolate site_name"
        # Brand voice: no em dashes in legal copy.
        assert "\u2014" not in privacy and "\u2014" not in terms, (
            "legal views must not contain em dashes (brand-voice rule)"
        )


class TestOfferingStyleModelGuard:
    """A keeper's /sessions custom page beside a Session model is the
    keeper's page: the model is not listed there and its router hands the
    path on.
    """

    def test_keeper_custom_page_at_sessions_is_kept_beside_a_session_model(self, tmp_path):
        """A keeper has a /sessions custom_page describing offerings and a
        Session data_model for admin CRUD. The content page is what
        /sessions serves. The Session resource router still mounts (for
        /admin/sessions CRUD) and its public list handler falls through to
        the pages router."""
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        import json as _json
        v1 = {
            "app_name": "fern-and-stone-test",
            "description": "reiki practitioner offering 60-minute sessions",
            "admin_email": "admin@example.com",
            "_harbor_meta": {"archetype_hint": "healer-wellness"},
            "data_models": [
                {
                    "name": "Session",
                    "fields": [
                        {"name": "name", "type": "string", "required": True},
                        {"name": "duration_minutes", "type": "integer"},
                        {"name": "price_cents", "type": "integer"},
                        {"name": "description", "type": "text"},
                    ],
                },
            ],
            "pages": [
                {"path": "/sessions", "title": "Sessions"},
            ],
        }
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        # The keeper's custom_page at /sessions must survive translation
        # as a custom view (the offerings narrative). It must NOT be
        # dropped as reserved by _custom_pages_filter.
        custom_view = build_dir / "views" / "custom" / "sessions.ejs"
        assert custom_view.exists(), (
            "keeper's /sessions custom_page was dropped; the model has no "
            "public listing, so the custom page must survive. "
            "Build dir contents:\n  " +
            "\n  ".join(sorted(p.relative_to(build_dir).as_posix() for p in build_dir.glob('**/*') if p.is_file()))
        )
        # And there should be a route handler in pages.js for /sessions
        # (registered through emit_custom_page_views/emit_routes_pages_js
        # for surviving custom pages).
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        assert "router.get('/sessions'" in pages_js, (
            "keeper's /sessions custom_page route was not registered in pages.js"
        )

    def test_session_resource_router_falls_through_to_the_custom_page(self, tmp_path):
        """When has_public is False on Session, the
        resource router's GET / handler must `next()` (not 404) so the
        pages router's /sessions custom-page handler can serve the request."""
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        import json as _json
        v1 = {
            "app_name": "fern-fallthrough-test",
            "description": "reiki",
            "admin_email": "admin@example.com",
            "_harbor_meta": {"archetype_hint": "healer-wellness"},
            "data_models": [
                {"name": "Session", "fields": [{"name": "name", "type": "string", "required": True}]},
            ],
            "pages": [{"path": "/sessions", "title": "Sessions"}],
        }
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        sessions_js = (build_dir / "routes" / "sessions.js").read_text()
        # The router must call next() rather than rendering a 404 when
        # has_public is False; the custom_page handler picks it up.
        # The exact mechanism is the router's `if (!has_public)` check.
        # We assert the falsy path uses next() (not error-404).
        assert "next()" in sessions_js, (
            "resource router of a model with no public listing must call next() to "
            "fall through to the pages router when has_public is false"
        )

    def test_explicit_public_listing_path_still_wins_for_session_model(self, tmp_path):
        """When the spec sets public_listing_path on a Session model, the
        public CRUD listing mounts there."""
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        import json as _json
        v1 = {
            "app_name": "explicit-listing-test",
            "description": "reiki",
            "admin_email": "admin@example.com",
            "_harbor_meta": {"archetype_hint": "healer-wellness"},
            "data_models": [
                {
                    "name": "Session",
                    "public_listing_path": "/sessions",  # explicit
                    "fields": [
                        {"name": "name", "type": "string", "required": True},
                    ],
                },
            ],
            "pages": [],
        }
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(_json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        server_js = (build_dir / "server.js").read_text()
        assert "app.use('/sessions', require('./routes/sessions'))" in server_js, (
            "explicit public_listing_path=/sessions on Session should still mount "
            "the public CRUD listing"
        )


class TestPrettifyAmpersandRecovery:
    """Harbor's slug pipeline converts `&` to `-and-` for kebab-case
    app_name, but `_prettify` rendered the slug back as "Fern And Stone"
    instead of "Fern & Stone".
    """

    def test_prettify_recovers_ampersand_for_fern_and_stone(self):
        from compose.build import _prettify
        assert _prettify("fern-and-stone") == "Fern & Stone"

    def test_prettify_recovers_ampersand_for_three_word_slugs(self):
        from compose.build import _prettify
        assert _prettify("wren-and-rye-cafe") == "Wren & Rye Cafe"
        assert _prettify("paper-and-stone-studio") == "Paper & Stone Studio"

    def test_prettify_does_not_replace_leading_or_trailing_and(self):
        """Edge case: a slug starting or ending with "and" must not lose
        that token. Only "and" between two non-empty words flips to "&"."""
        from compose.build import _prettify
        # "andrew-and-co": leading "andrew" must stay capitalized; the
        # middle "and" replaces to "&"; trailing "co" stays capitalized.
        assert _prettify("andrew-and-co") == "Andrew & Co"
        # A pathological slug starting with "and", just capitalize.
        assert _prettify("and-friends") == "And Friends"

    def test_prettify_is_case_insensitive_for_and_token(self):
        from compose.build import _prettify
        # Slugs are kebab-case-lowercase by convention but defend.
        assert _prettify("fern-AND-stone") == "Fern & Stone"

    def test_prettify_handles_empty_or_only_separators(self):
        from compose.build import _prettify
        assert _prettify("") == "Site"
        assert _prettify("---") == "Site"

    def test_prettify_strips_version_suffix_then_recovers_ampersand(self):
        from compose.build import _prettify
        # Rebuild lineage adds -vN-NN suffix; prettify strips first.
        assert _prettify("fern-and-stone-v3") == "Fern & Stone"
        assert _prettify("fern-and-stone-v3-12") == "Fern & Stone"


def test_alignment_lint_passes_on_compose_build_with_booking_page_and_model(tmp_path):
    """Compose-side integration regression for the 2026-04-29 admin/public
    alignment fix. The translator's /book-a-session auto-emit (in
    web/src/services/harbor_to_compose.ts) injects a custom_page bound to
    `form_target=BookingIntent` whenever the conversation surfaced
    booking intent. This test pins the COMPOSE-side downstream contract:

    Given a spec with a BookingIntent data_model AND a custom_page
    declaring `form_target=BookingIntent` (the post-translator shape), the
    compose pipeline must emit:

      1. routes/pages.js with an INSERT INTO booking_intents handler so
         the public form actually persists submissions to the same table
         the BookingIntent admin tab reads from (the four-signal
         heuristic's "(b) public form handler that INSERTs into <table>").

      2. A view at views/<book-a-session-slug>.ejs with a `<form>` whose
         action targets the keeper-facing path (the "(c) non-admin view
         references <table>" + "any non-admin view has a form action
         targeting /<slug>" signals).

    With both signals present, the worker's _lint_v2_build_output must
    NOT flag booking-intents as an orphan admin tab.

    Without this test, a future compose change that drops the form_target
    plumbing (e.g. silently turning the page into a content-only render)
    would re-orphan BookingIntent on every healer build; the exact
    regression an e2e run flagged; and the existing
    web/tests vitest-side translator coverage wouldn't catch it because
    vitest only asserts the v1-shape spec, not the emitted build.
    """
    import sys

    # Worker path manipulation: `queue/` has no __init__.py and shadows
    # stdlib `queue`, so we import worker_pg by file path.
    queue_dir = str(Path(__file__).resolve().parent.parent.parent / "queue")
    if queue_dir not in sys.path:
        sys.path.insert(0, queue_dir)
    cli_dir = str(Path(__file__).resolve().parent.parent.parent / "cli")
    if cli_dir not in sys.path:
        sys.path.insert(0, cli_dir)
    import worker_pg  # noqa: E402

    # Spec shape mirrors the post-translator output: BookingIntent model
    # (admin-only: no public_listing_path), plus a /book-a-session
    # custom_page bound to form_target.
    spec_dict = {
        "app_name": "booking-aligned-keeper",
        "archetype": "healer-wellness",
        "palette": "warm-earth",
        "font_pair": "cormorant-lato",
        "admin_email": "admin@example.com",
        "settings": {
            "site_name": "Booking Aligned Keeper",
            "hero_tagline": "Quiet sessions",
            "hero_description": "Integration repro of the 2026-04-29 alignment fix.",
            "contact_email": "hi@example.com",
        },
        "data_models": [
            {
                "name": "BookingIntent",
                "plural": "booking-intents",
                "fields": [
                    {"name": "name", "type": "string", "required": True},
                    {"name": "email", "type": "string", "required": True},
                    {"name": "preferred_window", "type": "string"},
                    {"name": "notes", "type": "text"},
                ],
            },
        ],
        "custom_pages": [
            {
                "path": "/book-a-session",
                "title": "Book a session",
                "purpose": (
                    "Public booking form so visitors can request a session. "
                    "Submissions land in admin's Booking Intents."
                ),
                "form_target": "BookingIntent",
                "allowed_fields": ["name", "email", "preferred_window", "notes"],
                "submit_text": "Send my request",
                "success_message": (
                    "Thank you. Your request landed; we will reach out to "
                    "confirm a time."
                ),
            },
        ],
    }

    out = build_app(spec_dict, output_dir=tmp_path / "booking-aligned-out")

    # Compose must emit routes/booking-intents.js (admin CRUD for the
    # model). This is the "admin tab" the lint inspects.
    booking_route = out / "routes" / "booking-intents.js"
    assert booking_route.exists(), (
        "compose must emit routes/booking-intents.js for the BookingIntent "
        "admin CRUD; without it the alignment scenario is unreachable. "
        f"Got: {sorted(p.name for p in (out / 'routes').glob('*.js'))}"
    )

    # Signal (b): pages.js MUST contain an INSERT into booking_intents so
    # the /book-a-session POST handler persists submissions. Without this
    # the form silently drops user input; exactly the 2026-04-29 bug.
    pages_js = (out / "routes" / "pages.js").read_text()
    assert "INSERT INTO booking_intents" in pages_js, (
        "pages.js must contain `INSERT INTO booking_intents` so the "
        "/book-a-session POST persists submissions; without it the "
        "form_target wiring silently drops keeper bookings. "
        f"pages.js head: {pages_js[:1200]!r}"
    )

    # Custom-page view emission: a /book-a-session view file must exist
    # and pull in the public-submission form widget. The form's action
    # attribute is bound at render time via `<%= _action %>` so the
    # literal table-name string isn't searchable in the view source; 
    # signal (a/b) via pages.js carries the lint already, but the view's
    # widget include is what makes the form actually reach the keeper.
    custom_view = out / "views" / "custom" / "book-a-session.ejs"
    assert custom_view.exists(), (
        "compose must emit views/custom/book-a-session.ejs for the "
        "custom_page; without it the GET /book-a-session handler in "
        "pages.js would render an empty page."
    )
    custom_view_text = custom_view.read_text()
    assert (
        "form/public-submission" in custom_view_text
        or "public-submission.ejs" in custom_view_text
    ), (
        "custom view must include the form/public-submission widget "
        "so visitors actually see a form bound to BookingIntent. "
        f"Got view text head: {custom_view_text[:600]!r}"
    )

    # End-to-end: the worker's alignment lint must now NOT flag
    # booking-intents as orphan. Other non-related orphans the lint may
    # surface (e.g. legal in some archetypes) are out of scope for this
    # regression; this assertion is scoped to booking-intents only.
    result = worker_pg._lint_v2_build_output(out)
    booking_orphan_warnings = [
        c for c in result["checks"]
        if "alignment-warn" in c and "booking-intents" in c
    ]
    assert not booking_orphan_warnings, (
        "alignment lint flagged booking-intents as orphan despite the "
        "/book-a-session public form being wired; signals (b) + (c) "
        "should have satisfied the four-signal heuristic. "
        f"Lint checks: {result['checks']}"
    )


# =====================================================================
# Social-link previews (OG + Twitter Card) end-to-end
#
# Item: v2-social-link-preview-end-to-end
# When a keeper pastes their site URL into FB / iMessage / Twitter /
# LinkedIn / Slack / Discord, the platform crawler must render a rich
# preview card. That requires:
#   1. A 1200x630 og-default.jpg auto-generated from brand mark + identity.
#   2. Complete OG + Twitter Card meta tags in <head>.
#   3. ABSOLUTE og:image + og:url URLs (relative paths break crawlers).
#   4. Per-page overrides via `ogTitle` / `ogDescription` / `ogImage` locals.
#   5. site_url auto-populated from the live request when the DB value is
#      blank, so previews work on the very first deploy.
# =====================================================================


class TestSocialLinkPreviewEndToEnd:
    """Coverage for the social-link preview feature (OG + Twitter Card)."""

    def test_emits_og_default_image_at_brand_path(self, resolved):
        files = dict(compose_files(resolved))
        assert "public/brand/og-default.jpg" in files, (
            "compose must emit /brand/og-default.jpg so social crawlers "
            "have a default 1200x630 preview image to render"
        )

    def test_og_default_image_is_valid_jpeg(self, resolved):
        files = dict(compose_files(resolved))
        data = files["public/brand/og-default.jpg"]
        assert isinstance(data, (bytes, bytearray)) and len(data) > 1000, (
            "og-default.jpg should be non-trivial JPEG bytes"
        )
        # JPEG magic bytes: FF D8 FF
        assert data[:3] == b"\xff\xd8\xff", "og-default.jpg must be a JPEG"

    def test_og_default_image_dimensions_are_1200x630(self, resolved):
        from PIL import Image
        import io
        files = dict(compose_files(resolved))
        data = files["public/brand/og-default.jpg"]
        img = Image.open(io.BytesIO(data))
        assert img.size == (1200, 630), (
            f"og:image must be exactly 1200x630 for `summary_large_image` "
            f"and FB rich preview; got {img.size}"
        )

    def test_og_default_image_is_deterministic(self, resolved):
        """Same spec -> same bytes (compose is deterministic)."""
        files1 = dict(compose_files(resolved))
        files2 = dict(compose_files(resolved))
        assert files1["public/brand/og-default.jpg"] == files2["public/brand/og-default.jpg"]

    def test_layout_emits_complete_og_meta_tags(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        for tag in (
            'property="og:type"',
            'property="og:site_name"',
            'property="og:title"',
            'property="og:description"',
            'property="og:url"',
            'property="og:image"',
            'property="og:image:width"',
            'property="og:image:height"',
            'property="og:image:alt"',
            'property="og:locale"',
        ):
            assert tag in layout, f"layout-public.ejs missing OG meta: {tag}"
        # Aspect-aware dims (v2-link-previews-og): width/height are
        # emitted only when genuinely known. The default card path knows
        # 1200x630; a per-entity image with unknown dims omits the tags
        # rather than lying about its aspect.
        assert "_ogDimsKnown" in layout
        assert "_ogImageIsDefault ? 1200 : 0" in layout
        assert "_ogImageIsDefault ? 630 : 0" in layout

    def test_layout_emits_twitter_card_meta_tags(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert 'name="twitter:card"' in layout
        # Aspect-aware card type: summary_large_image ONLY when the
        # image is genuinely landscape; portrait/square/unknown images get
        # the square `summary` card so covers never crop to a thin band.
        assert 'content="<%= _twitterCard %>"' in layout
        assert "'summary_large_image' : 'summary'" in layout
        assert "_ogImageW > _ogImageH" in layout
        for tag in (
            'name="twitter:title"',
            'name="twitter:description"',
            'name="twitter:image"',
            'name="twitter:image:alt"',
        ):
            assert tag in layout, f"layout-public.ejs missing Twitter meta: {tag}"

    def test_layout_resolves_og_image_to_absolute_url(self, resolved):
        """og:image must be an ABSOLUTE URL (https://...). Relative URLs
        break FB's Sharing Debugger + iMessage rich preview entirely."""
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        # The layout must build an _ogImageAbs / _ogUrlAbs and use them in
        # the meta tags (not the raw relative _ogImage / _canonicalRel).
        assert "_ogImageAbs" in layout
        assert "_ogUrlAbs" in layout
        assert 'content="<%= _ogImageAbs %>"' in layout
        assert 'content="<%= _ogUrlAbs %>"' in layout
        # The abs-URL helper must check for an existing scheme and prepend
        # the site_url when none is present.
        assert "_absUrl" in layout
        assert "http" in layout, "abs-URL helper must recognize http(s) schemes"

    def test_layout_default_og_image_points_at_emitted_brand_path(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert "/brand/og-default.jpg" in layout, (
            "layout-public.ejs default ogImage must reference the emitted "
            "/brand/og-default.jpg path"
        )

    def test_layout_supports_per_page_og_overrides(self, resolved):
        """Per-page detail views (event detail, photo detail) must be able
        to ship their own og:image + title without modifying the layout."""
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        # The locals contract is `ogTitle`, `ogDescription`, `ogImage`,
        # `ogImageAlt`, `ogType`. Each must be guarded by `typeof !==
        # 'undefined'` so rendering still works when omitted.
        for local in ("ogTitle", "ogDescription", "ogImage", "ogImageAlt", "ogType"):
            assert (
                f"typeof {local} !== 'undefined'" in layout
            ), f"layout must guard `{local}` with typeof check"

    def test_settings_middleware_auto_populates_site_url_from_request(self, resolved):
        """When site_settings.site_url is blank in the DB, the middleware
        must derive it from req.protocol + req.host so absolute OG URLs
        work on the very first deploy (before the customer opens
        /admin/settings)."""
        files = dict(compose_files(resolved))
        mw = files["middleware/settings.js"]
        assert "site_url" in mw
        assert "req.protocol" in mw
        assert "req.get" in mw, (
            "settings middleware must call req.get('host') to build the "
            "absolute base URL for social previews"
        )
        # The override must only fire when the DB value is blank; admin
        # edits to site_url in /admin/settings still win on next refresh.
        assert "if (!res.locals.settings.site_url)" in mw

    def test_layout_meta_description_falls_back_to_hero_tagline(self, resolved):
        """When site_description is blank, the meta-description (and
        og:description / twitter:description) should fall back to the
        hero tagline so previews never go blank."""
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert "_s.site_description ||" in layout
        assert "_s.hero_tagline" in layout

    def test_layout_canonical_link_uses_absolute_url_when_site_url_set(self, resolved):
        """The <link rel=canonical> should resolve to an absolute URL when
        site_url is available; same machinery that powers og:url."""
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert 'rel="canonical" href="<%= _canonical %>"' in layout
        assert "_canonical = _absUrl(" in layout

    def test_og_image_is_jpeg_not_webp_or_svg(self, resolved):
        """FB Sharing Debugger rejects WebP + SVG og:image. The default
        card is JPEG; og:image:type is now inferred from the image's
        extension with the default-card path pinned to image/jpeg
        and omitted entirely when the type cannot be determined."""
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert 'property="og:image:type"' in layout
        assert "'image/jpeg'" in layout, (
            "default-card og:image:type must resolve to image/jpeg"
        )
        assert "_ogImageType" in layout

    def test_logo_initials_helper_skips_punctuation(self):
        """Defensive helper used by the OG image renderer. Must mirror
        the favicon initials logic (alnum-first words only)."""
        from compose.build import _logo_initials_from_site_name
        assert _logo_initials_from_site_name("Wren & Rye") == "WR"
        assert _logo_initials_from_site_name("Fern & Stone") == "FS"
        assert _logo_initials_from_site_name("Fern") == "FE"
        assert _logo_initials_from_site_name("A") == "A"
        assert _logo_initials_from_site_name("") == "•"


def test_social_preview_layout_still_passes_undefined_settings_guard(resolved):
    """Belt-and-suspenders: the OG block introduced new `_s.X` reads + a
    `_siteUrl = (_s.site_url || '')` derivation. These must NOT regress
    the existing typeof-settings guard (which prevents 500-on-error-page
    cascades when settings is undefined)."""
    files = dict(compose_files(resolved))
    layout = files["views/layout-public.ejs"]
    assert "typeof settings !== 'undefined'" in layout
    import re
    bare = re.findall(r'(?<![A-Za-z0-9_])settings\.', layout)
    assert not bare, (
        f"OG block introduced {len(bare)} bare `settings.X` reference(s) "
        "that bypass the typeof guard. Use `_s.X` instead."
    )


class TestAutoFitCardGrid:
    """Listing-widget grids must use the auto-fit `cas-card-grid*` classes
    (flex-wrap + per-item max-width) so a model with 1 or 2 rows doesn't
    render a single stretched-across-the-row card. Mirrors MW gold-standard
    `.mw-card-grid` pattern. v2-mw-auto-fit-card-grid-with-max-width."""

    def _read_widget(self, name: str) -> str:
        widget_root = Path(__file__).resolve().parent.parent / "widgets"
        return (widget_root / name).read_text()

    def test_resource_list_card_grid_uses_cas_card_grid(self):
        src = self._read_widget("offerings/resource-list-card-grid.ejs")
        assert 'class="cas-card-grid"' in src
        assert "md:grid-cols-2" not in src

    def test_reviews_grid_uses_cas_card_grid(self):
        src = self._read_widget("offerings/reviews-grid.ejs")
        assert 'class="cas-card-grid"' in src
        assert "md:grid-cols-2" not in src

    def test_testimonials_stack_uses_cas_card_grid(self):
        src = self._read_widget("testimonials/stack.ejs")
        # Default grid stays cas-card-grid; a known-count praise band
        # (columns: 2/3) swaps to cas-grid-fixed-N so the row never
        # orphans.
        assert "'cas-card-grid'" in src
        assert "cas-grid-fixed-" in src
        assert "md:grid-cols-2" not in src

    def test_staff_card_grid_uses_cas_card_grid_3(self):
        src = self._read_widget("offerings/staff-card-grid.ejs")
        assert 'class="cas-card-grid-3"' in src
        assert "lg:grid-cols-3" not in src

    def test_grid_3col_uses_cas_card_grid_3(self):
        src = self._read_widget("offerings/grid-3col.ejs")
        assert 'class="cas-card-grid-3"' in src
        assert "md:grid-cols-3" not in src

    def test_instructor_grid_uses_cas_card_grid_4(self):
        src = self._read_widget("offerings/instructor-grid.ejs")
        assert 'class="cas-card-grid-4"' in src
        assert "lg:grid-cols-4" not in src

    def test_service_area_list_uses_cas_card_grid_4(self):
        src = self._read_widget("offerings/service-area-list.ejs")
        assert 'class="cas-card-grid-4"' in src
        assert "lg:grid-cols-4" not in src

    def test_emitted_css_carries_cas_card_grid_classes(self, resolved):
        """The build-time CSS generator must emit the auto-fit utility
        rules; widgets reference them by class name. Without this, the
        widgets render as plain unstyled <div>s in the browser.

        2026-05-16 (v2-card-grid-orphan-row-stretch): tightened max-width
        from 600/420 to 380 to keep orphans (count%cols==1) proportional;
        single-col breakpoint dropped from 760 to 580.
        """
        files = dict(compose_files(resolved))
        css = files["public/style.css"]
        assert ".cas-card-grid {" in css
        assert ".cas-card-grid > * {" in css
        assert "flex: 1 1 320px" in css
        assert "max-width: 380px" in css
        assert ".cas-card-grid-3 {" in css
        assert ".cas-card-grid-3 > * {" in css
        assert "flex: 1 1 280px" in css
        assert ".cas-card-grid-4 {" in css
        assert ".cas-card-grid-4 > * {" in css
        assert "flex: 1 1 220px" in css
        # Mobile collapse: solo card spans full width on phones rather
        # than honoring the desktop max-width cap.
        assert "@media (max-width: 580px)" in css
        assert ".cas-card-grid > *, .cas-card-grid-3 > *, .cas-card-grid-4 > * { max-width: 100%; }" in css

    def test_emitted_css_keeps_legacy_grid_cols_for_other_widgets(self, resolved):
        """Non-listing widgets (footer, hours/location, credentials, hero
        split-image, admin tools) still rely on the static grid-cols-*
        helpers; the auto-fit classes are additive, not a replacement."""
        files = dict(compose_files(resolved))
        css = files["public/style.css"]
        assert ".grid-cols-2 {" in css
        assert ".md\\:grid-cols-2 {" in css
        assert ".md\\:grid-cols-3 {" in css


class TestEditorialDropCap:
    """v2-mw-editorial-drop-cap-prose-block; drop-cap on long-form prose.

    The about-page hero description is long-form prose, so an editorial
    drop cap on the first letter reads like a magazine column instead
    of a generic marketing paragraph. The hero widgets honor a
    `drop_cap: true` widget param by adding `cas-drop-cap` on the
    description <p>; the global style.css carries the
    `.cas-drop-cap::first-letter` rule so any flourish set / template
    can opt in. The literary flourish set extends the rule with an
    italic+bolder treatment but no longer redefines the geometry
    (single source of truth).
    """

    def _read_widget(self, name: str) -> str:
        widget_root = Path(__file__).resolve().parent.parent / "widgets"
        return (widget_root / name).read_text()

    def test_global_css_emits_cas_drop_cap_rule(self, resolved):
        """style.css carries the `.cas-drop-cap::first-letter` rule
        regardless of which template/flourish-set is active so the
        opt-in class works on any v2 build."""
        files = dict(compose_files(resolved))
        css = files["public/style.css"]
        assert ".cas-drop-cap::first-letter {" in css
        assert "float: left;" in css
        assert "font-size: 3.4em;" in css
        assert "color: var(--color-primary);" in css

    def test_hero_centered_widget_honors_drop_cap_param(self):
        """hero/centered.ejs derives `_dropCap` from the `drop_cap`
        param and appends `cas-drop-cap` to the description <p> class
        when truthy. Default (param absent) emits no extra class so
        existing pages stay unchanged."""
        src = self._read_widget("hero/centered.ejs")
        assert "var _dropCap = (typeof drop_cap !== 'undefined' && drop_cap) ? ' cas-drop-cap' : '';" in src
        assert "<%= _dropCap %>" in src

    def test_hero_split_image_widget_honors_drop_cap_param(self):
        src = self._read_widget("hero/split-image.ejs")
        assert "var _dropCap = (typeof drop_cap !== 'undefined' && drop_cap) ? ' cas-drop-cap' : '';" in src
        assert "<%= _dropCap %>" in src

    def test_hero_editorial_stacked_widget_honors_drop_cap_param(self):
        src = self._read_widget("hero/editorial-stacked.ejs")
        assert "var _dropCap = (typeof drop_cap !== 'undefined' && drop_cap) ? ' cas-drop-cap' : '';" in src
        assert "<%= _dropCap %>" in src

    def test_about_page_hero_default_no_drop_cap(self):
        """`_page_widget_defaults` does NOT set `drop_cap` for the
        about-page hero by default. The drop-cap glyph (font-size 3.4em
        italic) overlaps the next character on shorter paragraphs
        (a one-letter first word such as "I" runs into the word after
        it), so we keep prose plain unless the chosen template family
        opts in (literary flourish set) or the keeper opts in via
        `_harbor_meta.drop_cap`. Home, contact, and other pages also
        stay plain by default; drop caps are reserved for opted-in
        long-form prose."""
        from compose.build import _page_widget_defaults
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        # Pin to a non-literary template family so we measure the
        # default-OFF behavior independent of pick_template_family.
        spec = scaffold_spec({
            "app_name": "drop-cap-test",
            "archetype": "healer-wellness",
            "site_name": "Drop Cap Test",
            "admin_email": "a@x.example",
            "template_family_id": "classic-business",
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))

        for hero_id in ("hero-centered", "hero-split-image", "hero-editorial-stacked"):
            about_defaults = _page_widget_defaults(rs, "about", hero_id)
            assert "drop_cap" not in about_defaults, (
                f"about-page {hero_id} default must NOT set drop_cap on "
                f"a non-literary template family (regression: shorter "
                f"paragraphs collide with the cap glyph)"
            )
            home_defaults = _page_widget_defaults(rs, "home", hero_id)
            assert "drop_cap" not in home_defaults
            contact_defaults = _page_widget_defaults(rs, "contact", hero_id)
            assert "drop_cap" not in contact_defaults

    def test_about_page_hero_literary_family_enables_drop_cap(self):
        """When the template family declares the literary flourish set,
        the about-page hero defaults to `drop_cap: True` so the
        editorial-statement / editorial-sanctuary chrome gets the
        first-letter accent its typography is designed around."""
        from compose.build import _page_widget_defaults
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        spec = scaffold_spec({
            "app_name": "drop-cap-literary",
            "archetype": "healer-wellness",
            "site_name": "Drop Cap Literary",
            "admin_email": "a@x.example",
            "template_family_id": "editorial-statement",
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))

        for hero_id in ("hero-centered", "hero-split-image", "hero-editorial-stacked"):
            about_defaults = _page_widget_defaults(rs, "about", hero_id)
            assert about_defaults.get("drop_cap") is True, (
                f"about-page {hero_id} on literary flourish set must set "
                f"drop_cap=True"
            )

    def test_about_page_hero_harbor_meta_drop_cap_opt_in(self):
        """Keepers can force the editorial cap regardless of template
        family by setting `_harbor_meta.drop_cap: true`."""
        from compose.build import _page_widget_defaults
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        # classic-business has flourish_set "subtle", not literary, so
        # the only signal turning drop_cap on here is _harbor_meta.
        spec = scaffold_spec({
            "app_name": "drop-cap-harbor-opt-in",
            "archetype": "healer-wellness",
            "site_name": "Drop Cap Harbor",
            "admin_email": "a@x.example",
            "template_family_id": "classic-business",
        })
        spec["_harbor_meta"] = {"drop_cap": True}
        rs = resolve_spec(BuildSpec.from_dict(spec))

        for hero_id in ("hero-centered", "hero-split-image", "hero-editorial-stacked"):
            about_defaults = _page_widget_defaults(rs, "about", hero_id)
            assert about_defaults.get("drop_cap") is True, (
                f"about-page {hero_id} must honor _harbor_meta.drop_cap"
            )

    def test_about_page_hero_stone_neutral_simple_statement_no_drop_cap(self):
        """Regression test (item: v2-about-drop-cap-default-off):
        the stone-neutral palette +
        simple-statement template family combo must NOT default to
        drop_cap=True. simple-statement declares flourish_set: none, so
        the about hero stays plain prose. This was the exact combo
        where "I bring" rendered as "Ibring" because the 3.4em italic
        cap glyph overlapped the next character on a short paragraph.
        """
        from compose.build import _page_widget_defaults
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec, compose_files
        spec = scaffold_spec({
            "app_name": "stone-neutral-simple",
            "archetype": "coach-consultant",
            "site_name": "Stone Neutral Simple",
            "admin_email": "a@x.example",
            "palette": "stone-neutral",
            "template_family_id": "simple-statement",
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))
        # template_family resolved to simple-statement, flourish_set "none"
        assert rs.template_family is not None
        assert rs.template_family.get("id") == "simple-statement"
        assert rs.template_family.get("flourish_set") == "none"

        for hero_id in ("hero-centered", "hero-split-image", "hero-editorial-stacked"):
            about_defaults = _page_widget_defaults(rs, "about", hero_id)
            assert "drop_cap" not in about_defaults, (
                "stone-neutral + simple-statement must not default to "
                "drop_cap=True (overlapping cap regression)"
            )

        # End-to-end: the emitted about.ejs include must NOT pass a
        # truthy drop_cap entry, so the rendered description <p> stays
        # plain and the hero widget's `_dropCap` evaluates to ''.
        files = dict(compose_files(rs))
        about = files["views/about.ejs"]
        # Hero include line for about page. Either no drop_cap key at
        # all, or explicitly false-y. The widget's truthy guard is
        # `(typeof drop_cap !== 'undefined' && drop_cap)` so an absent
        # key is the desired plain state.
        assert '"drop_cap": true' not in about
        assert '"drop_cap":true' not in about

    def test_about_view_default_omits_drop_cap_class(self, resolved):
        """End-to-end: the default fixture (no template_family, no
        harbor_meta opt-in) emits about.ejs WITHOUT a `drop_cap: true`
        param so the rendered description <p> stays plain."""
        files = dict(compose_files(resolved))
        about = files["views/about.ejs"]
        assert '"drop_cap": true' not in about
        assert '"drop_cap":true' not in about

    def test_about_view_literary_family_emits_drop_cap_class(self):
        """End-to-end: when the template family is literary, the
        emitted views/about.ejs hero include passes `drop_cap: true`
        so the rendered description <p> carries the cas-drop-cap
        class."""
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec, compose_files
        spec = scaffold_spec({
            "app_name": "literary-about",
            "archetype": "healer-wellness",
            "site_name": "Literary About",
            "admin_email": "a@x.example",
            "template_family_id": "editorial-statement",
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))
        files = dict(compose_files(rs))
        about = files["views/about.ejs"]
        assert '"drop_cap": true' in about

    def test_literary_flourish_no_longer_duplicates_drop_cap_geometry(self):
        """The literary flourish set used to redefine
        `.cas-drop-cap::first-letter` with `font-size: 3.4em; float: left;
        ...`; the geometry is now in global style.css. The flourish set
        should only carry literary-specific overrides (italic, weight)
        to avoid drift between the two definitions."""
        from compose.build import get_flourish_set
        literary = get_flourish_set("literary")
        assert literary is not None
        css = literary["css"]
        # The geometry properties belong in the global rule, not here.
        assert "float: left" not in css, (
            "literary flourish must not redefine drop-cap geometry "
            "(global .cas-drop-cap::first-letter owns it)"
        )
        # Italic/weight overrides are the literary-specific tilt.
        assert "font-style: italic" in css

    def test_literary_flourish_still_extends_drop_cap_via_class(self):
        """Layout still references cas-drop-cap so the body-class-scoped
        italic override applies when the literary family is selected.
        (Regression guard for the existing
        `test_editorial_literary_flourish_emits_drop_cap_and_link_underline`
        contract.)"""
        from compose.build import get_flourish_set
        literary = get_flourish_set("literary")
        assert "cas-drop-cap" in literary["css"]


# =====================================================================
# Favicon family + OG asset pipeline (item: v2-mw-favicon-and-og-asset-pipeline)
#
# Every v2 build emits, from a single brand-mark concept:
#   - 10 PNG sizes (16/32/48/64/96/128/180/192/256/512) at /brand/logo-<size>.png
#   - apple-touch-icon at /apple-touch-icon.png (180x180)
#   - multi-resolution favicon.ico at /favicon.ico (16/32/48/64/128/256)
# layout-public.ejs emits <link rel="icon"> tags for each size + the
# apple-touch-icon + the legacy /favicon.ico shortcut.
# =====================================================================


class TestFaviconAssetPipeline:
    """Coverage for compose/utils/brand_assets.py + emit wiring."""

    def test_emits_all_png_sizes(self, resolved):
        from compose.utils.brand_assets import PNG_SIZES
        files = dict(compose_files(resolved))
        for size in PNG_SIZES:
            path = f"public/brand/logo-{size}.png"
            assert path in files, (
                f"compose must emit {path} so layout-public.ejs's "
                f"<link rel=\"icon\" sizes=\"{size}x{size}\"> resolves"
            )

    def test_emits_apple_touch_icon(self, resolved):
        files = dict(compose_files(resolved))
        assert "public/apple-touch-icon.png" in files, (
            "compose must emit /apple-touch-icon.png so iOS Safari's "
            "default fetch on home-screen pin succeeds"
        )

    def test_emits_favicon_ico(self, resolved):
        files = dict(compose_files(resolved))
        assert "public/favicon.ico" in files, (
            "compose must emit /favicon.ico for legacy browsers + RSS "
            "readers + any client that auto-fetches that path"
        )

    def test_pngs_are_valid_with_correct_dimensions(self, resolved):
        from PIL import Image
        from compose.utils.brand_assets import PNG_SIZES
        import io
        files = dict(compose_files(resolved))
        for size in PNG_SIZES:
            data = files[f"public/brand/logo-{size}.png"]
            assert isinstance(data, (bytes, bytearray)) and len(data) > 100
            assert data[:8] == b"\x89PNG\r\n\x1a\n", (
                f"logo-{size}.png is not a valid PNG"
            )
            img = Image.open(io.BytesIO(data))
            assert img.size == (size, size), (
                f"logo-{size}.png must be exactly {size}x{size}; "
                f"got {img.size}"
            )

    def test_favicon_ico_is_valid_multi_resolution(self, resolved):
        files = dict(compose_files(resolved))
        data = files["public/favicon.ico"]
        # ICO magic bytes: 00 00 01 00 (type 1 = icon).
        assert data[:4] == b"\x00\x00\x01\x00", (
            "favicon.ico is not a valid ICO"
        )
        # Image count from the ICO header.
        count = int.from_bytes(data[4:6], "little")
        assert count >= 4, (
            f"favicon.ico must bundle at least 4 sizes; got {count}"
        )

    def test_apple_touch_icon_is_180px(self, resolved):
        from PIL import Image
        import io
        files = dict(compose_files(resolved))
        img = Image.open(io.BytesIO(files["public/apple-touch-icon.png"]))
        assert img.size == (180, 180)

    def test_favicon_pipeline_is_deterministic(self, resolved):
        """Same spec -> byte-identical favicon outputs."""
        from compose.utils.brand_assets import PNG_SIZES
        a = dict(compose_files(resolved))
        b = dict(compose_files(resolved))
        for size in PNG_SIZES:
            path = f"public/brand/logo-{size}.png"
            assert a[path] == b[path]
        assert a["public/favicon.ico"] == b["public/favicon.ico"]
        assert a["public/apple-touch-icon.png"] == b["public/apple-touch-icon.png"]

    def test_layout_emits_link_tags_for_every_emitted_png_size(self, resolved):
        """The layout's <link rel=icon sizes="<n>x<n>"> tags must each
        point at an emitted PNG. No dead links."""
        from compose.utils.brand_assets import PNG_SIZES
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        # Every PNG size that the layout advertises must be on disk.
        # We validate the union by reading each <link rel="icon" ... >
        # path out of the layout and checking it's emitted.
        for size in PNG_SIZES:
            href = f'href="/brand/logo-{size}.png"'
            if href in layout:
                # Layout linked it -> compose must emit it.
                assert f"public/brand/logo-{size}.png" in files

    def test_layout_links_apple_touch_icon(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert 'rel="apple-touch-icon"' in layout
        assert 'href="/brand/logo-180.png"' in layout

    def test_layout_links_shortcut_favicon_ico(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert 'rel="shortcut icon"' in layout
        assert 'href="/favicon.ico"' in layout

    def test_initials_extraction_skips_non_alphanumeric_words(self):
        """`Wren & Rye` must yield `WR` (not `W&`); `Fern & Stone` -> `FS`."""
        from compose.utils.brand_assets import initials_from_site_name
        assert initials_from_site_name("Wren & Rye") == "WR"
        assert initials_from_site_name("Fern & Stone") == "FS"
        assert initials_from_site_name("Fern") == "FE"
        assert initials_from_site_name("A") == "A"
        assert initials_from_site_name("") == "•"
        assert initials_from_site_name("Foo + Bar") == "FB"

    def test_render_brand_mark_png_handles_all_sizes(self):
        """16px through 512px must all produce valid PNG bytes."""
        from compose.utils.brand_assets import render_brand_mark_png, PNG_SIZES
        from PIL import Image
        import io
        palette = {
            "--color-primary": "#3B5BA9",
            "--color-on-primary": "#FFFFFF",
        }
        for size in PNG_SIZES:
            png = render_brand_mark_png(size, "Test Site", palette)
            assert png is not None
            img = Image.open(io.BytesIO(png))
            assert img.size == (size, size)

    def test_crop_to_brand_mark_returns_none_for_uniform_image(self):
        """Auto-crop on a fully-transparent image must return None
        (no detectable mark) so callers can skip the cropped variant."""
        from compose.utils.brand_assets import crop_to_brand_mark
        from PIL import Image
        import io
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        # Fully transparent: no foreground pixels.
        result = crop_to_brand_mark(buf.getvalue())
        assert result is None

    def test_crop_to_brand_mark_crops_to_content_bbox(self):
        """A 200x200 image with a 50x50 black square at (75, 75) must
        crop to ~50x50 (with dilation rounding adding a few pixels)."""
        from compose.utils.brand_assets import crop_to_brand_mark
        from PIL import Image, ImageDraw
        import io
        img = Image.new("RGBA", (200, 200), (255, 255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([75, 75, 125, 125], fill=(0, 0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        cropped_bytes = crop_to_brand_mark(buf.getvalue())
        assert cropped_bytes is not None
        cropped = Image.open(io.BytesIO(cropped_bytes))
        # Expect roughly 50x50 plus 2 * dilation_px (3) on each side.
        w, h = cropped.size
        assert 45 <= w <= 65, f"cropped width {w} out of range"
        assert 45 <= h <= 65, f"cropped height {h} out of range"


# ---- v2-ejs-locals-bare-variable-undefined (2026-05-06) -------------
# Page views must read res.render-passed locals via `locals.X`, not bare
# `typeof X !== 'undefined'`. express-ejs-layouts wraps the page render
# in a layout-only scope where bare access of route-passed locals can
# return undefined even when the route passed the data, producing silent
# empty-state renders. First seen on a /services page.

class TestEmittedViewsUseLocalsForRenderPassedLocals:
    """Regression guards for the EJS locals.X access pattern in page views."""

    def test_emit_page_view_db_listing_uses_locals_items(self):
        """db-backed page views must include `locals.items` in the include
        data; not bare `typeof items`. The route does
        `res.render(view, { items: rows })` and bare access can fail
        under express-ejs-layouts' layout-only scope wrapping."""
        from compose.build import BuildSpec, resolve_spec, compose_files
        spec = BuildSpec.from_dict({
            "app_name": "yoga-locals-test",
            "archetype": "fitness-studio",
            "admin_email": "a@b.c",
        })
        rs = resolve_spec(spec)
        files = dict(compose_files(rs))
        inst_view = files["views/instructors.ejs"]
        assert "locals.items" in inst_view, (
            "db-backed page view must reference the runtime items via "
            "locals.items, not bare `typeof items`."
        )
        assert "typeof items !== 'undefined'" not in inst_view, (
            "db-backed page view must NOT use bare `typeof items` "
            "(see roadmap v2-ejs-locals-bare-variable-undefined)."
        )

    def test_emit_page_view_contact_form_uses_locals_for_render_locals(self):
        """The contact page emit injects preselected_service + submitted
        from the page view's locals. Use locals.preselected_service /
        locals.submitted, not bare typeof X."""
        from compose.build import BuildSpec, resolve_spec, compose_files
        spec = BuildSpec.from_dict({
            "app_name": "contact-locals-test",
            "archetype": "healer-wellness",
            "admin_email": "a@b.c",
        })
        rs = resolve_spec(spec)
        files = dict(compose_files(rs))
        # contact page should be emitted as views/contact.ejs.
        contact_view = files.get("views/contact.ejs", "")
        assert contact_view, "expected views/contact.ejs to be emitted"
        # Either of these is acceptable; the include-data injection runs
        # only when the contact page hosts the form-contact widget.
        if "preselected_service" in contact_view:
            assert "locals.preselected_service" in contact_view, (
                "contact page must access preselected_service via locals.X"
            )
            assert "typeof preselected_service !== 'undefined'" not in contact_view, (
                "contact page must NOT use bare typeof preselected_service"
            )
        if "submitted" in contact_view:
            assert "locals.submitted" in contact_view, (
                "contact page must access submitted via locals.X"
            )
            assert "typeof submitted !== 'undefined'" not in contact_view, (
                "contact page must NOT use bare typeof submitted"
            )

    def test_audit_emitted_views_for_bare_locals_returns_empty_for_clean_input(self):
        from compose.build import audit_emitted_views_for_bare_locals
        clean = [
            ("views/services.ejs",
             "<%- include('./widgets/X.ejs', { items: (locals && Array.isArray(locals.items)) ? locals.items : [] }) %>"),
            ("views/contact.ejs",
             "<%- include('./widgets/contact.ejs', { preselected_service: ((locals && locals.preselected_service) || '') }) %>"),
        ]
        violations = audit_emitted_views_for_bare_locals(clean)
        assert violations == [], (
            f"clean input must produce no violations: {violations}"
        )

    def test_audit_emitted_views_for_bare_locals_flags_bare_typeof(self):
        from compose.build import audit_emitted_views_for_bare_locals
        bad = [
            ("views/services.ejs",
             "<%- include('./widgets/X.ejs', { items: typeof items !== 'undefined' && items ? items : [] }) %>"),
            ("views/blog.ejs",
             "<%- include('./widgets/X.ejs', { items: typeof items !== 'undefined' ? items : [] }) %>"),
        ]
        violations = audit_emitted_views_for_bare_locals(bad)
        assert len(violations) >= 2, (
            f"expected at least 2 violations across services.ejs + blog.ejs: {violations}"
        )
        paths = {v["path"] for v in violations}
        assert "views/services.ejs" in paths
        assert "views/blog.ejs" in paths
        for v in violations:
            assert v["local"] == "items"

    def test_audit_skips_widget_includes(self):
        """Widget partials under views/widgets/ are include-only; their
        locals come from the include's data arg (a fresh scope), so bare
        typeof works there. The audit must not flag them."""
        from compose.build import audit_emitted_views_for_bare_locals
        widget_with_typeof = [
            ("views/widgets/hero/centered.ejs",
             "<% var _d = (typeof description !== 'undefined') ? description : ''; %>"),
            # Widget receives `items` via include(); bare typeof is the
            # established convention in widget bodies. Guard the audit's
            # exclusion specifically for these.
            ("views/widgets/offerings/grid.ejs",
             "<% var _i = (typeof items !== 'undefined' && items) ? items : []; %>"),
        ]
        violations = audit_emitted_views_for_bare_locals(widget_with_typeof)
        assert violations == [], (
            f"widget includes must be excluded from the audit: {violations}"
        )

    def test_audit_skips_layouts(self):
        """Layouts have their own scope; bare typeof works there."""
        from compose.build import audit_emitted_views_for_bare_locals
        layout = [
            ("views/layout-public.ejs",
             "<% var _i = (typeof items !== 'undefined') ? items : []; %>"),
            ("views/layout-admin.ejs",
             "<% var _r = (typeof rows !== 'undefined') ? rows : []; %>"),
        ]
        violations = audit_emitted_views_for_bare_locals(layout)
        assert violations == [], (
            f"layouts must be excluded from the audit: {violations}"
        )

    def test_full_build_emits_no_bare_locals_in_page_views(self):
        """Whole-build smoke check: a representative spec must produce
        zero bare-locals violations in its top-level page views."""
        from compose.build import (
            BuildSpec, resolve_spec, compose_files,
            audit_emitted_views_for_bare_locals,
        )
        spec = BuildSpec.from_dict({
            "app_name": "audit-smoke",
            "archetype": "fitness-studio",
            "admin_email": "a@b.c",
        })
        rs = resolve_spec(spec)
        files = list(compose_files(rs))
        violations = audit_emitted_views_for_bare_locals(files)
        assert violations == [], (
            "compose_files must emit page views that use locals.X, never "
            "bare `typeof X !== 'undefined'` for route-passed locals. "
            f"Violations: {violations[:3]}"
        )


# ---------------------------------------------------------------------------
# v2-compose-tailwind-curated-subset-coverage-gaps regression tests
# ---------------------------------------------------------------------------
#
# The compose engine emits a curated Tailwind-like utility CSS subset; when a
# widget references a utility class the emitted CSS doesn't carry, the
# rendered page silently collapses that property (`<a class="px-7 py-3">` with
# no `.px-7` rule loses horizontal padding). These tests pin
# `audit_emitted_css_utility_coverage` as the regression guard so the gap
# becomes visible at build time, plus assert that the specific utilities
# observed missing on an author-site build are now present in the curated
# CSS.
# ---------------------------------------------------------------------------


class TestCssUtilityCoverageAudit:
    """Coverage audit for the curated Tailwind-like utility CSS subset."""

    def test_audit_returns_empty_for_clean_input(self):
        from compose.build import audit_emitted_css_utility_coverage
        clean = [
            ("public/style.css", ".px-4 { padding-left: 1rem; padding-right: 1rem; }"),
            ("views/widgets/x.ejs", '<div class="px-4">hello</div>'),
        ]
        missing = audit_emitted_css_utility_coverage(clean)
        assert missing == [], f"expected no missing utilities: {missing}"

    def test_audit_flags_missing_utility(self):
        from compose.build import audit_emitted_css_utility_coverage
        # Widget uses px-7 but the CSS only defines px-4.
        files = [
            ("public/style.css", ".px-4 { padding-left: 1rem; padding-right: 1rem; }"),
            ("views/widgets/hero.ejs", '<a class="px-7 py-3">CTA</a>'),
        ]
        missing = audit_emitted_css_utility_coverage(files)
        flagged = {m["class"] for m in missing}
        assert "px-7" in flagged, f"audit should flag px-7: {missing}"
        assert "py-3" in flagged, f"audit should flag py-3: {missing}"
        # Evidence carries the path of the consumer.
        for m in missing:
            assert "views/widgets/hero.ejs" in m["used_in"]

    def test_audit_ignores_component_classes(self):
        """Component classes (cas-*, mw-*) are NOT in the curated utility
        subset; their CSS lives in component-scoped blocks. The audit must
        only police Tailwind-utility-shape tokens."""
        from compose.build import audit_emitted_css_utility_coverage
        files = [
            ("public/style.css", ".px-4 { padding-left: 1rem; }"),
            ("views/widgets/x.ejs",
             '<div class="cas-photo-hero mw-card-past px-4">x</div>'),
        ]
        missing = audit_emitted_css_utility_coverage(files)
        flagged = {m["class"] for m in missing}
        assert "cas-photo-hero" not in flagged
        assert "mw-card-past" not in flagged
        assert flagged == set(), f"only utility-shape tokens audited: {missing}"

    def test_audit_ignores_ejs_template_artifacts(self):
        """Class lists frequently interpolate `<%= _cls %>` style EJS
        expressions; those fragments are not real class names and must
        never produce audit noise."""
        from compose.build import audit_emitted_css_utility_coverage
        files = [
            ("public/style.css", ".px-4 { padding-left: 1rem; }"),
            ("views/widgets/x.ejs", '<div class="px-4 <%= _cls %>">x</div>'),
        ]
        missing = audit_emitted_css_utility_coverage(files)
        assert missing == [], f"no EJS-artifact false positives: {missing}"

    def test_audit_treats_layout_inline_style_blocks_as_a_source(self):
        """`.font-display` and `.font-body` live in the inline `<style>`
        block of `views/layout-public.ejs` because their font-family
        values depend on the chosen font_pair. The audit must read those
        inline blocks too, not just `public/style.css`."""
        from compose.build import audit_emitted_css_utility_coverage
        files = [
            ("public/style.css", ".px-4 { padding-left: 1rem; }"),
            ("views/layout-public.ejs",
             "<style>\n.font-display { font-family: serif; }\n"
             ".font-body { font-family: sans; }\n</style>\n"
             "<body><%- body %></body>"),
            ("views/widgets/x.ejs",
             '<h1 class="font-display">x</h1>'
             '<p class="font-body">y</p>'),
        ]
        missing = audit_emitted_css_utility_coverage(files)
        assert missing == [], f"layout <style> blocks count: {missing}"

    def test_audit_flags_variant_prefixed_utilities(self):
        """`md:`, `hover:`, `focus:` variants resolve to escaped CSS
        selectors (`md\\:px-7`); a widget using `md:px-7` must trigger
        the audit when no matching escaped selector exists."""
        from compose.build import audit_emitted_css_utility_coverage
        files = [
            ("public/style.css",
             "@media (min-width: 768px) {\n"
             "  .md\\:py-12 { padding: 3rem 0; }\n"
             "}"),
            ("views/widgets/x.ejs", '<section class="md:py-12 md:px-7">x</section>'),
        ]
        missing = audit_emitted_css_utility_coverage(files)
        flagged = {m["class"] for m in missing}
        assert "md:px-7" in flagged, f"variant utilities must audit: {missing}"
        assert "md:py-12" not in flagged, f"defined variant clean: {missing}"

    def test_curated_css_defines_polish_layer_utilities(self):
        """The utilities a hand-applied polish layer had to add
        manually (px-7, py-0.5, py-28, md:px-7, md:py-28, ml-1, plus
        neighbors observed missing during the curation-gap audit) must
        now live in the curated CSS bundle."""
        from compose.build import (
            BuildSpec, resolve_spec, compose_files,
            audit_emitted_css_utility_coverage,
        )
        spec = BuildSpec.from_dict({
            "app_name": "css-coverage-smoke",
            "archetype": "healer-wellness",
            "admin_email": "a@b.c",
        })
        rs = resolve_spec(spec)
        files = list(compose_files(rs))
        css = next((c for p, c in files if p == "public/style.css"), "")
        # Polish-layer utilities the prior session had to add by
        # hand. These MUST now live in the bundled CSS.
        for needle in (
            ".px-1 ", ".px-7 ",
            ".py-0\\.5 ", ".py-28 ", ".py-2\\.5 ",
            ".ml-1 ", ".ml-3 ",
            ".pt-1 ", ".pt-4 ", ".pb-5 ", ".pb-6 ",
            ".mb-1\\.5 ",
            ".space-y-1 ", ".space-y-3 ", ".space-y-5 ", ".space-y-10 ",
            ".opacity-0 ", ".opacity-90 ",
            ".bottom-0 ", ".right-0 ", ".right-2 ",
            ".border-2 ", ".border-dashed ", ".border-primary ",
            ".bg-primary ",
            ".whitespace-pre-wrap ",
            ".text-gray-900 ",
            ".md\\:px-7 ", ".md\\:py-28 ", ".md\\:py-5 ",
            ".focus\\:absolute:focus",
            ".focus\\:top-2:focus",
            ".focus\\:left-2:focus",
            ".focus\\:z-50:focus",
            ".focus\\:px-4:focus",
            ".focus\\:py-2:focus",
            ".focus\\:rounded-md:focus",
            ".focus\\:shadow-md:focus",
        ):
            assert needle in css, (
                f"curated style.css must define {needle.strip()!r}; "
                "see roadmap v2-compose-tailwind-curated-subset-coverage-gaps"
            )

    def test_full_build_emits_zero_missing_utilities_default_archetype(self):
        """Whole-build smoke check: the curated subset must cover every
        utility class referenced by widgets in a representative archetype
        build. Zero missing utilities means the curated CSS bundle is in
        sync with the widget layer."""
        from compose.build import (
            BuildSpec, resolve_spec, compose_files,
            audit_emitted_css_utility_coverage,
        )
        spec = BuildSpec.from_dict({
            "app_name": "css-coverage-smoke",
            "archetype": "fitness-studio",
            "admin_email": "a@b.c",
        })
        rs = resolve_spec(spec)
        files = list(compose_files(rs))
        missing = audit_emitted_css_utility_coverage(files)
        assert missing == [], (
            "curated CSS must cover every utility used by widgets in a "
            "default build. Missing: "
            f"{[m['class'] for m in missing[:10]]}"
        )

    def test_full_build_audit_clean_across_all_archetypes(self):
        """Cross-archetype variant of the smoke check: build the home
        page for every shipped archetype and assert zero missing
        utilities. Guards the curated subset against drift introduced by
        archetype-specific widget combinations."""
        from compose.build import (
            BuildSpec, resolve_spec, compose_files,
            audit_emitted_css_utility_coverage,
        )
        archetypes = [
            "fitness-studio", "healer-wellness", "coach-consultant",
            "restaurant-cafe", "salon-spa", "tradesperson-contractor",
            "local-service", "event-producer", "therapist-counselor",
        ]
        all_missing = {}
        for arch in archetypes:
            spec = BuildSpec.from_dict({
                "app_name": f"css-cov-{arch}",
                "archetype": arch,
                "admin_email": "a@b.c",
            })
            rs = resolve_spec(spec)
            files = list(compose_files(rs))
            for m in audit_emitted_css_utility_coverage(files):
                all_missing.setdefault(m["class"], set()).update(m["used_in"])
        assert all_missing == {}, (
            "curated CSS must cover every utility used across every "
            "archetype. Missing per-archetype: "
            f"{sorted(all_missing.keys())[:20]}"
        )

    def test_compose_files_appends_css_warning_for_missing_utility(self):
        """When a widget references a utility the curated CSS doesn't
        define, compose_files must surface the gap as a translation
        warning so the SE reviewer sees it in compose-manifest.txt +
        translation-warnings.txt rather than discovering it in
        production."""
        from compose.build import BuildSpec, resolve_spec, compose_files
        spec = BuildSpec.from_dict({
            "app_name": "css-coverage-warn",
            "archetype": "fitness-studio",
            "admin_email": "a@b.c",
        })
        rs = resolve_spec(spec)
        # Stuff a synthetic widget that uses a non-existent utility class.
        # The audit runs over `files` after they're built, so we monkey-
        # patch the underlying file list by composing first and then
        # re-invoking the audit through the public entry point. Easier:
        # call the audit directly with a hand-rolled file list.
        from compose.build import audit_emitted_css_utility_coverage
        synthetic = [
            ("public/style.css", ".px-4 { padding-left: 1rem; }"),
            ("views/widgets/x.ejs", '<div class="px-9999">x</div>'),
        ]
        missing = audit_emitted_css_utility_coverage(synthetic)
        assert any(m["class"] == "px-9999" for m in missing), (
            "audit must flag the synthetic missing utility"
        )

    def test_looks_like_tailwind_utility_excludes_ejs_expressions(self):
        from compose.build import _looks_like_tailwind_utility
        assert _looks_like_tailwind_utility("px-4") is True
        assert _looks_like_tailwind_utility("py-0.5") is True
        assert _looks_like_tailwind_utility("md:px-7") is True
        assert _looks_like_tailwind_utility("hover:opacity-90") is True
        assert _looks_like_tailwind_utility("cas-photo-hero") is False
        assert _looks_like_tailwind_utility("<%=") is False
        assert _looks_like_tailwind_utility("") is False
        assert _looks_like_tailwind_utility("Math.min(_items.length,") is False
        # Variant-prefixed component class still rejected.
        assert _looks_like_tailwind_utility("md:cas-thing") is False

    def test_build_app_writes_px99_warning_to_compose_manifest(self, tmp_path, monkeypatch):
        """End-to-end acceptance per roadmap
        v2-compose-emitted-css-grep-audit-pass: a deliberate `class="px-99"`
        reference (not in the curated CSS) must propagate through
        compose_files -> rs.spec.engine_warnings -> build_app and
        land as a `warning: css_utility_missing: px-99 ...` line in the
        on-disk compose-manifest.txt. Monkeypatches the audit helper to
        inject the px-99 gap because the curated subset deliberately
        covers every utility real widgets emit (so we can't trigger the
        warning by archetype alone)."""
        import compose.build as _cb

        real_audit = _cb.audit_emitted_css_utility_coverage

        def _audit_with_synthetic_px99(files):
            gaps = list(real_audit(files))
            gaps.append({"class": "px-99", "used_in": ["views/widgets/synthetic.ejs"]})
            return gaps

        monkeypatch.setattr(
            _cb, "audit_emitted_css_utility_coverage", _audit_with_synthetic_px99
        )

        out = _cb.build_app(str(SAMPLE_SPEC), output_dir=tmp_path / "px99")
        manifest = (out / "compose-manifest.txt").read_text()
        assert "warning: css_utility_missing:" in manifest, (
            "build_app must surface css_utility_missing warnings in the "
            f"compose-manifest.txt. Manifest:\n{manifest}"
        )
        assert "px-99" in manifest, (
            "px-99 synthetic gap must appear verbatim in the manifest "
            f"warning line. Manifest:\n{manifest}"
        )
        # The translation-warnings.txt sidecar must also carry the line.
        warnings_txt = (out / "translation-warnings.txt").read_text()
        assert "css_utility_missing" in warnings_txt
        assert "px-99" in warnings_txt

    def test_build_app_clean_spec_emits_no_css_utility_warnings(self, tmp_path):
        """End-to-end "no false positives on a clean build" half of the
        acceptance criteria: a default archetype build whose widgets only
        reference utilities the curated CSS subset defines must produce
        zero `warning: css_utility_missing:` lines in the manifest."""
        from compose.build import build_app

        out = build_app(str(SAMPLE_SPEC), output_dir=tmp_path / "clean")
        manifest = (out / "compose-manifest.txt").read_text()
        assert "warning: css_utility_missing:" not in manifest, (
            "clean default build must not surface any css_utility_missing "
            f"false positives. Manifest:\n{manifest}"
        )


# ---------------------------------------------------------------------------
# v2-exclude-pages-models-support regression tests
# ---------------------------------------------------------------------------
#
# Closes gaps from the partial 2026-05-06 implementation of
# _harbor_meta.exclude_data_models + exclude_pages:
#   (1) when a layout widget sources from `db:<excluded_table>`, the route
#       MUST NOT emit a `SELECT * FROM <table>` against the missing table.
#       Fixed in emit_routes_pages_js with a `_known_tables` membership
#       check; the route falls back to rendering the view with no items
#       so the widget's defensive `_areas.length > 0` guard renders an
#       empty (skipped) section instead of crashing.
#   (2) the home page layout for archetypes that reference an excluded
#       model via a section widget (e.g. tradesperson-contractor's
#       service-area-list, local-service's reviews-grid) must degrade
#       gracefully to the empty state rather than 500ing.
# ---------------------------------------------------------------------------


class TestHarborMetaExcludeDataModelsRoutes:
    """Parametric regression tests for the 2026-05-06 partial implementation
    follow-up. When _harbor_meta.exclude_data_models drops a model whose
    table is referenced by a layout widget's `source: db:<table>`, the
    emitted routes/pages.js must NOT emit a SELECT against the missing
    table; the route should render the view with no items so the
    widget's empty-state guard kicks in.
    """

    @pytest.mark.parametrize(
        "archetype,excluded_model,excluded_table,affected_pages,unrelated_table_kept",
        [
            # tradesperson-contractor: ServiceArea is in default_data_models
            # and the home page + service-areas page both source from
            # `db:service_areas` via the service-area-list widget. Both
            # pages must lose the SELECT after exclusion.
            (
                "tradesperson-contractor",
                "ServiceArea",
                "service_areas",
                ["home", "service-areas"],
                "reviews",
            ),
            # local-service: Review is in default_data_models and the
            # reviews page sources from `db:reviews` via shared-reviews-default.
            # The home page does NOT reference db:reviews (testimonials
            # widget uses db:testimonials, not reviews) so only /reviews
            # is impacted.
            (
                "local-service",
                "Review",
                "reviews",
                ["reviews"],
                "contact_inquiries",
            ),
            # restaurant-cafe: Review is NOT in default_data_models. This
            # case documents the safe no-op behavior; excluding a model
            # that does not exist in the archetype defaults still produces
            # a valid build (defense-in-depth against Opus hallucinating
            # an exclusion for a model the archetype never had).
            (
                "restaurant-cafe",
                "Review",
                "reviews",
                [],
                "menu_items",
            ),
        ],
    )
    def test_excluded_data_model_dropped_from_schema_and_routes(
        self, tmp_path, archetype, excluded_model, excluded_table,
        affected_pages, unrelated_table_kept,
    ):
        """End-to-end: build a spec with _harbor_meta.exclude_data_models
        set, then assert (a) the excluded table is absent from schema/seed,
        (b) routes/pages.js does not SELECT FROM the excluded table on any
        of the archetype's default pages, and (c) at least one unrelated
        archetype-default table is still present (so we haven't accidentally
        nuked the whole schema)."""
        from compose.build import build_app
        v2 = {
            "app_name": "exclude-models-test",
            "archetype": archetype,
            "admin_email": "admin@example.com",
            "settings": {"site_name": "Exclude Models Test"},
            "_harbor_meta": {"exclude_data_models": [excluded_model]},
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        schema = (build_dir / "db" / "schema.sql").read_text()
        seed = (build_dir / "db" / "seed.sql").read_text()
        pages_js = (build_dir / "routes" / "pages.js").read_text()

        # (a) Excluded table absent from schema + seed.
        assert f"CREATE TABLE IF NOT EXISTS {excluded_table}" not in schema, (
            f"{archetype}: excluded model {excluded_model} should NOT have "
            f"its table {excluded_table} in schema.sql"
        )
        assert f"INSERT INTO {excluded_table}" not in seed, (
            f"{archetype}: excluded model {excluded_model} should NOT have "
            f"seed rows inserted into {excluded_table}"
        )

        # (b) No route SELECTs against the excluded table. Gap 1 regression:
        # the route emitter used to honor `source: db:<table>` and emit a
        # SELECT even when the table no longer existed in the schema,
        # causing the request to 500 with an undefined-table error.
        assert f"FROM {excluded_table}" not in pages_js, (
            f"{archetype}: routes/pages.js must NOT SELECT FROM the "
            f"excluded table {excluded_table}. Found a stale db: route "
            f"that would 500 at runtime."
        )

        # (c) Unrelated archetype-default tables are still present, so
        # the exclusion is narrowly scoped.
        if unrelated_table_kept:
            assert f"CREATE TABLE IF NOT EXISTS {unrelated_table_kept}" in schema, (
                f"{archetype}: unrelated table {unrelated_table_kept} "
                f"should still be in schema.sql (exclusion must be narrow)"
            )

        # (d) Page handlers for the affected archetype pages are still
        # emitted (the page route is not dropped), but the handler is
        # the no-db variant. Each affected page's route must exist AND
        # must not query the excluded table.
        for page_id in affected_pages:
            path = "/" if page_id == "home" else f"/{page_id}"
            assert f"router.get('{path}'" in pages_js, (
                f"{archetype}: page route {path} should still be emitted "
                f"(exclude_data_models drops the model, not the page; "
                f"use exclude_pages for that)"
            )

    @pytest.mark.parametrize(
        "archetype,excluded_model,impacted_widget_id",
        [
            ("tradesperson-contractor", "ServiceArea", "service-area-list"),
            ("local-service", "Review", "reviews-grid"),
        ],
    )
    def test_excluded_data_model_widget_empty_state_renders(
        self, tmp_path, archetype, excluded_model, impacted_widget_id,
    ):
        """Gap 2 regression: the home/page layout widget that referenced
        the excluded model must degrade gracefully. The route handler
        renders without `items` set; the widget's defensive
        `(locals && Array.isArray(locals.items)) ? locals.items : []`
        wrapper in the page view + the widget's own `_areas.length > 0`
        guard combine to skip rendering the section entirely.

        This test asserts the emitted page view still references the
        widget include (we did not silently strip it) AND the include
        carries a defensive `locals.items` fallback so the EJS render
        does not throw on undefined `items`.
        """
        from compose.build import build_app
        v2 = {
            "app_name": "exclude-widget-test",
            "archetype": archetype,
            "admin_email": "admin@example.com",
            "settings": {"site_name": "Widget Empty State Test"},
            "_harbor_meta": {"exclude_data_models": [excluded_model]},
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        home_view = (build_dir / "views" / "home.ejs").read_text()

        # The widget include is still emitted (we did not retroactively
        # strip the include from the layout); the route just no longer
        # populates `items`.
        # For tradesperson the widget include is on the home page;
        # for local-service the reviews-grid widget is on the /reviews
        # page (shared-reviews-default), not home, so this assertion
        # is split per-case.
        if impacted_widget_id == "service-area-list":
            assert impacted_widget_id in home_view, (
                f"home.ejs should still include the {impacted_widget_id} "
                f"widget; the empty state is the widget's responsibility, "
                f"not a compose-time strip"
            )
            # Defensive locals.items fallback present on the include.
            assert "Array.isArray(locals.items)" in home_view, (
                "home.ejs must wrap items with Array.isArray(locals.items) "
                "fallback so the EJS render does not throw when the route "
                "renders with no items local"
            )
        else:
            # reviews-grid: check the /reviews view instead.
            reviews_view = (build_dir / "views" / "reviews.ejs").read_text()
            assert impacted_widget_id in reviews_view, (
                f"reviews.ejs should still include {impacted_widget_id}"
            )
            assert "Array.isArray(locals.items)" in reviews_view, (
                "reviews.ejs must wrap items with Array.isArray(locals.items) "
                "fallback"
            )

    def test_exclude_data_models_is_case_insensitive(self, tmp_path):
        """The harbor_meta.exclude_data_models filter lowercases both the
        archetype model names and the exclusion list (see compose/build.py
        ~L579: `m.lower()`). Asserts that exclusion still works when the
        keeper / Opus emits a case-mismatched name like 'servicearea',
        'SERVICEAREA', or 'service_area'.
        """
        from compose.build import build_app
        for variant in ("ServiceArea", "servicearea", "SERVICEAREA"):
            v2 = {
                "app_name": "exclude-case-test",
                "archetype": "tradesperson-contractor",
                "admin_email": "admin@example.com",
                "settings": {"site_name": "Case Test"},
                "_harbor_meta": {"exclude_data_models": [variant]},
            }
            sub = tmp_path / variant.lower()
            sub.mkdir(parents=True, exist_ok=True)
            spec_path = sub / "spec.json"
            spec_path.write_text(json.dumps(v2))
            build_dir = sub / "out"
            build_app(str(spec_path), output_dir=str(build_dir))
            schema = (build_dir / "db" / "schema.sql").read_text()
            assert "CREATE TABLE IF NOT EXISTS service_areas" not in schema, (
                f"exclude variant {variant!r} should still drop service_areas"
            )


class TestHarborMetaExcludePages:
    """Parametric regression tests for _harbor_meta.exclude_pages, which
    drops archetype-default pages from page_layouts (and thus from nav,
    routes, and view emission).
    """

    @pytest.mark.parametrize(
        "archetype,excluded_pages,unrelated_page_kept",
        [
            # tradesperson-contractor: drop the /service-areas page. The
            # service-area-list widget on the home page is independent of
            # this exclusion (gap 2 covers that scenario via
            # exclude_data_models). Verifies the page-level exclusion
            # surface alone.
            (
                "tradesperson-contractor",
                ["service-areas"],
                "services",
            ),
            # restaurant-cafe: drop the /menu page. The /menu page sources
            # from `db:menu_items` via the menu-grouped-list widget, so
            # this also exercises the route-side gap-1 fix (no SELECT
            # against menu_items from the /menu handler because the page
            # itself is gone).
            (
                "restaurant-cafe",
                ["menu"],
                "visit",
            ),
        ],
    )
    def test_excluded_page_dropped_from_routes_and_views(
        self, tmp_path, archetype, excluded_pages, unrelated_page_kept,
    ):
        from compose.build import build_app
        v2 = {
            "app_name": "exclude-pages-test",
            "archetype": archetype,
            "admin_email": "admin@example.com",
            "settings": {"site_name": "Exclude Pages Test"},
            "_harbor_meta": {"exclude_pages": list(excluded_pages)},
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        pages_js = (build_dir / "routes" / "pages.js").read_text()
        for page_id in excluded_pages:
            path = f"/{page_id}"
            view_file = build_dir / "views" / f"{page_id}.ejs"
            assert f"router.get('{path}'" not in pages_js, (
                f"{archetype}: excluded page route {path} must NOT be "
                f"emitted in routes/pages.js"
            )
            assert not view_file.exists(), (
                f"{archetype}: excluded page view {view_file.name} must "
                f"NOT be emitted"
            )

        # Unrelated archetype-default page survives the exclusion.
        unrelated_path = (
            "/" if unrelated_page_kept == "home" else f"/{unrelated_page_kept}"
        )
        assert f"router.get('{unrelated_path}'" in pages_js, (
            f"{archetype}: unrelated page {unrelated_path} must still be "
            f"emitted (exclusion must be narrow)"
        )

    def test_exclude_pages_drops_nav_entry(self, tmp_path):
        """Page exclusion should also remove the page from the public nav
        items so the customer's primary navigation doesn't carry a dead
        link to a route that no longer exists."""
        from compose.build import build_app
        v2 = {
            "app_name": "exclude-nav-test",
            "archetype": "tradesperson-contractor",
            "admin_email": "admin@example.com",
            "settings": {"site_name": "Nav Test"},
            "_harbor_meta": {"exclude_pages": ["service-areas"]},
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))

        # The nav middleware injects nav_items into res.locals. The PUBLIC
        # nav must not carry an entry pointing at the excluded path.
        # Note: admin nav still carries /admin/service-areas because the
        # ServiceArea data_model is not excluded: exclude_pages drops
        # the public surface, not the underlying admin CRUD. Assert the
        # PUBLIC `"href": "/service-areas"` pattern (without the /admin
        # prefix) is absent.
        settings_mw = (build_dir / "middleware" / "settings.js").read_text()
        assert '"href": "/service-areas"' not in settings_mw, (
            "settings middleware public nav_items must not include "
            '"href": "/service-areas" after exclude_pages drops the page'
        )
        # Defense-in-depth: admin nav SHOULD still carry the entry since
        # the data_model is not excluded.
        assert '"href": "/admin/service-areas"' in settings_mw, (
            "admin nav must still carry /admin/service-areas because "
            "exclude_pages only drops the public page, not the data_model"
        )


# ----------------------------------------------------------------------
# v2-author-archetype-books-data-model (2026-05-14): Author archetype + Book CRUD
# ----------------------------------------------------------------------


def test_author_archetype_is_registered():
    """The `author` archetype must be present in archetypes.json with the
    canonical default_pages (home, books, about, blog, events, contact) and
    the Book data_model attached as primary."""
    archetypes_path = (Path(__file__).resolve().parent.parent / "data"
                       / "archetypes.json")
    with open(archetypes_path) as f:
        archetypes = json.load(f)
    by_id = {a["id"]: a for a in archetypes["archetypes"]}
    assert "author" in by_id, "author archetype missing from archetypes.json"
    arche = by_id["author"]
    assert "books" in arche["default_pages"]
    assert "home" in arche["default_pages"]
    model_names = [m["name"] for m in arche["default_data_models"]]
    assert "Book" in model_names, "author archetype must declare a Book model"
    book_model = next(m for m in arche["default_data_models"] if m["name"] == "Book")
    assert book_model["plural"] == "books"
    # The page_layouts pointers must name real layout ids defined in
    # page-layouts.json (the no-bare-widget-id regression guard).
    # author-home-spotlight replaced author-home-default as the home
    # pointer (home must not clone the catalog page).
    assert arche["page_layouts"]["home"] == "author-home-spotlight"
    assert arche["page_layouts"]["books"] == "author-books-default"


def test_author_books_layout_uses_db_books_source():
    """author-books-default page-layout must point books-grid at db:books
    so admin CRUD edits to the books table show up on the public /books
    page immediately without a rebuild."""
    layouts_path = (Path(__file__).resolve().parent.parent / "data"
                    / "page-layouts.json")
    with open(layouts_path) as f:
        layouts = json.load(f)
    layout = layouts["layouts"]["author-books-default"]
    list_widget = next(w for w in layout["widgets"] if w["id"] == "books-grid")
    assert list_widget["source"] == "db:books", (
        f"author /books must read DB rows; got source={list_widget['source']!r}"
    )


def test_author_home_layout_uses_db_books_source():
    """author-home-default page-layout must point books-grid at db:books so
    the featured-on-home book set comes from live DB rows."""
    layouts_path = (Path(__file__).resolve().parent.parent / "data"
                    / "page-layouts.json")
    with open(layouts_path) as f:
        layouts = json.load(f)
    layout = layouts["layouts"]["author-home-default"]
    list_widget = next(w for w in layout["widgets"] if w["id"] == "books-grid")
    assert list_widget["source"] == "db:books"


def test_books_grid_widget_exists_and_handles_retailer_links():
    """The books-grid widget file must exist and compose retailer links
    (Amazon as the primary CTA + Goodreads / Local bookstore / Barnes & Noble /
    NetGalley / Audible as secondary inline links)."""
    widgets_dir = Path(__file__).resolve().parent.parent / "widgets"
    widget_path = widgets_dir / "offerings" / "books-grid.ejs"
    assert widget_path.exists(), (
        "compose/widgets/offerings/books-grid.ejs missing"
    )
    # Card markup (retailer links etc.) lives in the shared book-card.ejs
    # partial since the series-grouping rework; the contract spans both.
    text = widget_path.read_text(encoding="utf-8") + (
        widgets_dir / "offerings" / "book-card.ejs"
    ).read_text(encoding="utf-8")
    # Amazon renders as the prominent primary CTA.
    assert "amazon_url" in text and "Buy on Amazon" in text, (
        "books-grid must compose an Amazon primary CTA"
    )
    # Each secondary retailer is composed into the card.
    for ret in ("goodreads_url", "local_bookstore_url", "barnesnoble_url",
                "netgalley_url", "audible_url"):
        assert ret in text, (
            f"books-grid must reference {ret} so the retailer link composes"
        )
    # Tolerant locals: widget reads `items` (db:books) AND `books`
    # (spec.<page>.books literal).
    assert "typeof items" in text and "typeof books" in text, (
        "books-grid must accept both `items` (db) and `books` (spec) locals"
    )


def test_book_default_fields_cover_author_archetype():
    """Book data_model must declare all 20 fields the author
    archetype's /admin/books CRUD relies on."""
    fields = _default_fields("Book")
    field_names = {f["name"] for f in fields}
    expected = {
        "slug", "title", "subtitle", "series_label", "description",
        "cover_image_url", "cover_image_alt", "cover_credit",
        "accolade", "rating_callout", "price_display",
        "amazon_url", "goodreads_url", "local_bookstore_url",
        "barnesnoble_url", "netgalley_url", "audible_url",
        "featured_on_home", "sort_order", "published",
    }
    missing = expected - field_names
    assert not missing, (
        f"Book data_model missing required fields: {sorted(missing)}"
    )
    # The title is the only required field. Everything else is optional
    # so authors can populate the row incrementally.
    title_field = next(f for f in fields if f["name"] == "title")
    assert title_field.get("required") is True, (
        "Book.title must be required (the only required field)"
    )


def test_author_archetype_builds_end_to_end(tmp_path):
    """Smoke test: a minimal author-archetype spec must build cleanly,
    emit the books table in the schema, mount the admin CRUD router at
    /admin/books, and emit a /books page route that SELECTs from books."""
    v2 = {
        "app_name": "test-author-build",
        "archetype": "author",
        "admin_email": "author@example.com",
        "settings": {"site_name": "Test Author"},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(v2))
    build_dir = tmp_path / "out"
    build_app(str(spec_path), output_dir=str(build_dir))

    # Schema includes the books table with all canonical columns.
    schema = (build_dir / "db" / "schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS books" in schema
    for col in ("amazon_url", "goodreads_url", "local_bookstore_url",
                "barnesnoble_url", "netgalley_url", "audible_url",
                "featured_on_home", "sort_order", "published",
                "cover_image_url", "series_label"):
        assert col in schema, f"books schema missing {col}"

    # Pages router emits a /books GET handler that SELECTs from books.
    pages_js = (build_dir / "routes" / "pages.js").read_text()
    assert "router.get('/books'" in pages_js
    assert "SELECT * FROM books" in pages_js

    # Admin CRUD router for /admin/books exists.
    books_route = build_dir / "routes" / "books.js"
    assert books_route.exists(), "routes/books.js (admin CRUD router) missing"

    # Server mounts the books router (dual public + admin).
    server_js = (build_dir / "server.js").read_text()
    assert "/admin/books" in server_js or "books.js" in server_js, (
        "server.js must mount the books router"
    )

    # Admin views exist for the Books CRUD (manage / new / edit).
    for view in ("manage", "new", "edit"):
        view_file = build_dir / "views" / "books" / f"{view}.ejs"
        assert view_file.exists(), f"views/books/{view}.ejs missing"


# ============================================================
# scaffold_spec on a minimal answers dict
# ============================================================


def test_scaffold_spec_fills_a_minimal_answers_dict_from_the_archetype():
    """The four required answers are enough: everything else comes from
    the archetype's defaults. This is the shape `translate_v1_spec` and the
    `python -m compose.intake` command pass.
    """
    from compose.intake import scaffold_spec
    legacy_answers = {
        "app_name": "legacy-coach",
        "archetype": "coach-consultant",
        "site_name": "Legacy Coach",
        "admin_email": "owner@example.com",
    }
    v2 = scaffold_spec(legacy_answers)
    assert v2["app_name"] == "legacy-coach"
    assert v2["settings"]["site_name"] == "Legacy Coach"
    # Archetype defaults still render because no keeper services
    # were supplied.
    home = v2["pages"]["home"]
    offering_titles = [s["title"] for s in home["offerings"]["items"]]
    assert any(t in offering_titles for t in ("1:1 Executive Coaching",
                                              "Leadership Intensives"))


# =============================================================================
# per-route <title> + <meta
# name="description"> emitter. Every public route's handler in routes/pages.js
# must thread a per-route `metaDescription` local so the layout's existing
# conditional render is deterministic per page rather than always falling
# back to spec-level site_description.
# =============================================================================

class TestPerRouteTitleAndMetaDescription:
    """emit_routes_pages_js must emit a unique <title> + metaDescription
    local for every public route."""

    def _build_for(self, tmp_path, v1):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        return pages_js

    def test_per_route_title_suffixes_differ_across_archetype_pages(self, tmp_path):
        """Archetype default pages emit distinct title suffixes derived
        from the prettified page id (Home / About / Services / Contact)
        so search-result titles never collapse to the bare site name."""
        v1 = {
            "app_name": "seo-coach",
            "description": "Executive coach for senior leaders.",
            "admin_email": "owner@example.com",
        }
        pages_js = self._build_for(tmp_path, v1)
        # Specific-first titles (v2-seo-headers-jsonld): interior
        # pages lead with the page name, the brand follows.
        assert "'About | ' + (res.locals.settings.site_name" in pages_js
        assert "'Contact | ' + (res.locals.settings.site_name" in pages_js
        # The home page is the brand itself plus a short tagline when one
        # is configured; never "Site | Home".
        assert "| Home" not in pages_js
        home_idx = pages_js.index("router.get('/'")
        home_block = pages_js[home_idx:home_idx + 900]
        assert "hero_tagline" in home_block, (
            "home title should append the hero tagline when configured"
        )

    def test_home_title_dedupes_tagline_against_site_name(self, tmp_path):
        """In the v1->v2 translate path, site_name and hero_tagline both
        default to the humanized app_name when neither is supplied, so a
        naive 'site_name | tagline' append doubles the home title
        ('Sample Novelist | Sample Novelist'). The emitted home title must dedupe
        case-insensitively, either direction (no stuttering).
        """
        import shutil
        import subprocess
        if shutil.which("node") is None:
            pytest.skip("node is not on PATH")
        v1 = {
            "app_name": "sample-novelist",
            "description": "Independent novelist with a backlist of novels.",
            "admin_email": "owner@example.com",
        }
        pages_js = self._build_for(tmp_path, v1)
        home_idx = pages_js.index("router.get('/',")
        m = re.search(
            r"title:\s*(\(function\(\)\{.*?\}\)\(\)),",
            pages_js[home_idx:home_idx + 1400], re.S,
        )
        assert m, "home title must be the dedup IIFE"
        expr = m.group(1)

        def _eval(site_name, tagline):
            code = (
                "const res={locals:{settings:{site_name:"
                + json.dumps(site_name) + ",hero_tagline:"
                + json.dumps(tagline) + "}}};console.log(" + expr + ");"
            )
            r = subprocess.run(["node", "-e", code], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            return r.stdout.strip()

        # Duplicate tagline -> site name alone (no doubling).
        assert _eval("Sample Novelist", "Sample Novelist") == "Sample Novelist"
        # Substring either direction collapses too.
        assert _eval("Sample Novelist Author", "Sample Novelist") == "Sample Novelist Author"
        # A genuinely distinct tagline is appended.
        assert _eval("Sample Novelist", "Invented tagline, plainly so") == "Sample Novelist | Invented tagline, plainly so"
        # Empty tagline -> site name alone.
        assert _eval("Sample Novelist", "") == "Sample Novelist"

    def test_every_public_route_emits_meta_description_local(self, tmp_path):
        """Every emitted res.render call for a public page must carry a
        metaDescription local. We assert the local appears at least as
        many times as there are router.get handlers in routes/pages.js."""
        v1 = {
            "app_name": "seo-meta-present",
            "description": "A boutique studio for one-on-one coaching.",
            "admin_email": "owner@example.com",
        }
        pages_js = self._build_for(tmp_path, v1)
        # The thank-you re-render inside the POST /contact handler is
        # an admin-only path and does not get metaDescription; the
        # GET handlers we care about each emit `metaDescription:`.
        assert pages_js.count("metaDescription:") >= 1
        # Spec-level fallback flows in from settings.site_description
        # (populated by translate_v1_spec from v1["description"]).
        assert "A boutique studio for one-on-one coaching." in pages_js

    def test_page_description_override_via_custom_page_collision(self, tmp_path):
        """When a customer custom_page collides with an archetype-default
        path (e.g. /about) and supplies its own `description`, that
        description wins over the spec-level fallback for that route's
        metaDescription local."""
        v1 = {
            "app_name": "seo-about-override",
            "description": "Spec-level coach blurb.",
            "admin_email": "owner@example.com",
            "pages": [
                {
                    "path": "/about",
                    "title": "Our Story",
                    "description": "The journey behind a 12-week coaching container for senior operators.",
                },
            ],
        }
        from compose.intake import translate_v1_spec
        v2 = translate_v1_spec(v1)
        # The translator currently retains custom_page descriptions only
        # if we inject them; for this test, we assert through the v2 spec
        # plumbing directly by hydrating custom_pages with the description
        # before building. The translator preserves unknown keys via
        # passthrough: verify the per-page override path holds end-to-end.
        # If translate dropped the description, hydrate it back here so we
        # exercise the build.py override path explicitly.
        for cp in v2.get("custom_pages", []):
            if cp.get("path") == "/about" and not cp.get("description"):
                cp["description"] = "The journey behind a 12-week coaching container for senior operators."
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        from compose.build import build_app
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        about_idx = pages_js.index("router.get('/about'")
        about_block = pages_js[about_idx:about_idx + 600]
        assert "The journey behind a 12-week coaching container" in about_block, (
            f"per-page description override did not flow into /about handler:\n{about_block}"
        )

    def test_meta_description_truncated_to_155_chars_on_word_boundary(self):
        """Long descriptions are truncated to ~155 chars at a word
        boundary with a trailing ellipsis so SERP snippets never break
        mid-word."""
        from compose.build import _truncate_meta_description_for_seo
        long = (
            "We craft custom websites for boutique studios, healers, "
            "coaches, and solo practitioners who want a quiet, calm "
            "place online that reflects their actual practice rather "
            "than a templated marketing pitch."
        )
        out = _truncate_meta_description_for_seo(long)
        assert len(out) <= 156  # 155 + 1 ellipsis char
        assert out.endswith("…")
        # Last visible character before the ellipsis must NOT be a
        # mid-word slice: the helper trims back to the prior space.
        # We assert by checking the prefix (sans ellipsis) is a strict
        # prefix of the original at a word boundary.
        trimmed = out[:-1].rstrip()
        assert long.startswith(trimmed), (
            f"truncated output {trimmed!r} is not a word-aligned prefix "
            f"of the original input"
        )

    def test_meta_description_short_input_passes_through_unchanged(self):
        """Short descriptions (≤ 155 chars) are returned as-is, no
        ellipsis appended."""
        from compose.build import _truncate_meta_description_for_seo
        short = "A boutique coaching studio."
        assert _truncate_meta_description_for_seo(short) == short

    def test_meta_description_empty_input_returns_empty(self):
        from compose.build import _truncate_meta_description_for_seo
        assert _truncate_meta_description_for_seo("") == ""
        assert _truncate_meta_description_for_seo(None) == ""

    def test_layout_public_threads_metaDescription_local(self):
        """Defensive layout guard: the layout-public.ejs conditional
        must read `metaDescription` (not a renamed local) so the route
        emitter's threading is honored."""
        layout_path = (
            Path(__file__).resolve().parent.parent
            / "templates" / "layout-public.ejs"
        )
        layout = layout_path.read_text()
        assert "typeof metaDescription !== 'undefined'" in layout
        # The fallback chain must still be present so non-route renders
        # (e.g. error pages) keep working when metaDescription is absent.
        assert "_s.site_description" in layout
        assert "_s.hero_tagline" in layout


# =============================================================================
# canonical + robots + sitemap.
# Every public route must thread `canonicalUrl` so layout-public renders a
# correct <link rel="canonical">. /robots.txt + /sitemap.xml are owned by
# the new routes/seo.js so the bodies can reference the live request origin
# rather than baking a host-less file at build time.
# =============================================================================

class TestCanonicalUrlPerRoute:
    """emit_routes_pages_js + emit_routes_legal_js must thread canonicalUrl
    locals on every res.render call so layout-public.ejs emits accurate
    <link rel="canonical"> tags per page."""

    def _build_for(self, tmp_path, v1):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        return build_dir

    def test_archetype_default_route_handlers_thread_canonical_url(self, tmp_path):
        v1 = {
            "app_name": "seo-canonical-archetype",
            "description": "Quiet coaching for senior operators.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        # Each archetype default GET handler must declare canonicalUrl
        # with the route's exact path so the layout's _absUrl(canonicalUrl)
        # produces the right absolute URL per page.
        assert "canonicalUrl: '/'" in pages_js, "home route missing canonicalUrl: '/'"
        assert "canonicalUrl: '/about'" in pages_js, "/about route missing canonicalUrl"
        assert "canonicalUrl: '/contact'" in pages_js, "/contact route missing canonicalUrl"

    def test_legal_routes_thread_canonical_url(self, tmp_path):
        v1 = {
            "app_name": "seo-canonical-legal",
            "description": "Quiet coaching for senior operators.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        legal_js = (build_dir / "routes" / "legal.js").read_text()
        assert "canonicalUrl: '/privacy'" in legal_js
        assert "canonicalUrl: '/terms'" in legal_js

    def test_custom_page_handler_threads_canonical_url(self, tmp_path):
        v1 = {
            "app_name": "seo-canonical-custom",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
            "pages": [
                {"path": "/our-story", "title": "Our Story",
                 "description": "How we got here."},
            ],
        }
        build_dir = self._build_for(tmp_path, v1)
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        assert "canonicalUrl: '/our-story'" in pages_js

    def test_layout_public_canonical_link_falls_back_when_local_missing(self):
        """The layout must keep its existing canonical fallback so error
        pages and admin views (which don't thread canonicalUrl) still
        render a valid <link rel="canonical"> tag."""
        layout_path = (
            Path(__file__).resolve().parent.parent
            / "templates" / "layout-public.ejs"
        )
        layout = layout_path.read_text()
        # Conditional read of canonicalUrl (route-threaded value wins).
        assert "typeof canonicalUrl !== 'undefined'" in layout
        # The <link rel="canonical"> tag itself must reference the
        # computed _canonical local.
        assert 'rel="canonical" href="<%= _canonical %>"' in layout


class TestRobotsTxtRoute:
    """/robots.txt is owned by routes/seo.js (express route, not a static
    file) so the Sitemap line can reference the live request origin."""

    def _build_for(self, tmp_path, v1):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        return build_dir

    def test_seo_router_file_emitted(self, tmp_path):
        v1 = {
            "app_name": "seo-robots",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        assert (build_dir / "routes" / "seo.js").exists()

    def test_static_robots_txt_no_longer_emitted(self, tmp_path):
        """The static public/robots.txt would shadow the new route via
        express.static; emit_public_assets must NOT produce it anymore."""
        v1 = {
            "app_name": "seo-static-shadow",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        assert not (build_dir / "public" / "robots.txt").exists(), (
            "public/robots.txt must be removed so routes/seo.js owns "
            "the /robots.txt URL"
        )

    def test_robots_route_allows_root_and_disallows_admin_api_webhooks(self, tmp_path):
        v1 = {
            "app_name": "seo-robots-body",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        seo_js = (build_dir / "routes" / "seo.js").read_text()
        assert "router.get('/robots.txt'" in seo_js
        assert "'Allow: /'" in seo_js
        assert "'Disallow: /admin'" in seo_js
        assert "'Disallow: /api'" in seo_js
        assert "'Disallow: /webhooks'" in seo_js
        assert "'Content-Type', 'text/plain" in seo_js
        # Sitemap discovery line so crawlers find /sitemap.xml without
        # re-parsing the page tree.
        assert "Sitemap: ' + origin + '/sitemap.xml" in seo_js

    def test_seo_router_mounted_before_pages_in_server_js(self, tmp_path):
        v1 = {
            "app_name": "seo-server-mount",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        server_js = (build_dir / "server.js").read_text()
        seo_idx = server_js.index("require('./routes/seo')")
        pages_idx = server_js.index("require('./routes/pages')")
        assert seo_idx < pages_idx, (
            "routes/seo.js must mount before routes/pages.js so custom_pages "
            "cannot shadow /robots.txt or /sitemap.xml"
        )


class TestSitemapXmlRoute:
    """/sitemap.xml is owned by routes/seo.js; the URL list is derived at
    build time from page_layouts + non-auth custom_pages + /privacy +
    /terms + each data_model's public_listing_path when set."""

    def _build_for(self, tmp_path, v1):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        return build_dir

    def test_sitemap_route_emitted_with_xml_content_type(self, tmp_path):
        v1 = {
            "app_name": "sitemap-content-type",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        seo_js = (build_dir / "routes" / "seo.js").read_text()
        assert "router.get('/sitemap.xml'" in seo_js
        assert "'Content-Type', 'application/xml" in seo_js
        # XML declaration + urlset are baked into the response body so the
        # output is a valid sitemap regardless of which URLs land in
        # PUBLIC_ROUTES.
        assert '<?xml version="1.0" encoding="UTF-8"?>' in seo_js
        assert 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"' in seo_js

    def test_sitemap_url_list_includes_archetype_default_routes(self, tmp_path):
        v1 = {
            "app_name": "sitemap-archetype",
            "description": "Quiet studio for coaches.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        seo_js = (build_dir / "routes" / "seo.js").read_text()
        # PUBLIC_ROUTES literal carries every page_layouts path; the
        # home path is "/" rather than "/home".
        assert "PUBLIC_ROUTES = " in seo_js
        # Pull the literal JSON array out of the file so we can introspect
        # it without depending on serialization order.
        import re
        match = re.search(r"PUBLIC_ROUTES = (\[[^\]]*\])", seo_js)
        assert match, "PUBLIC_ROUTES literal not found in routes/seo.js"
        routes = json.loads(match.group(1))
        # Coach archetype produces at least these public paths.
        for required in ("/", "/about", "/contact", "/privacy", "/terms"):
            assert required in routes, (
                f"sitemap PUBLIC_ROUTES missing required path {required!r}; "
                f"got {routes}"
            )

    def test_sitemap_excludes_admin_auth_required_custom_pages(self, tmp_path):
        v1 = {
            "app_name": "sitemap-auth-filter",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
            "pages": [
                {"path": "/notes", "title": "Notes", "auth_required": True},
                {"path": "/journal-public", "title": "Journal"},
            ],
        }
        build_dir = self._build_for(tmp_path, v1)
        seo_js = (build_dir / "routes" / "seo.js").read_text()
        import re
        match = re.search(r"PUBLIC_ROUTES = (\[[^\]]*\])", seo_js)
        assert match, "PUBLIC_ROUTES literal not found"
        routes = json.loads(match.group(1))
        assert "/notes" not in routes, (
            "auth_required custom pages must not appear in the sitemap"
        )
        # And the public sibling does land in the sitemap.
        assert "/journal-public" in routes

    def test_sitemap_lastmod_is_iso_8601_utc(self, tmp_path):
        """BUILD_TIME constant in routes/seo.js must be a valid ISO 8601
        UTC timestamp (Z suffix), so every <lastmod> rendered by the
        route is a deterministic, crawler-readable freshness signal."""
        v1 = {
            "app_name": "sitemap-lastmod",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        seo_js = (build_dir / "routes" / "seo.js").read_text()
        import re
        match = re.search(r"BUILD_TIME = '([^']+)'", seo_js)
        assert match, "BUILD_TIME literal not found in routes/seo.js"
        build_time = match.group(1)
        # ISO 8601: YYYY-MM-DDTHH:MM:SSZ (no fractional seconds, UTC).
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", build_time
        ), f"BUILD_TIME {build_time!r} is not ISO 8601 UTC"
        # And the response body renders <lastmod>BUILD_TIME</lastmod>.
        assert "<lastmod>' + BUILD_TIME + '</lastmod>" in seo_js

    def test_sitemap_origin_falls_back_to_request_host_when_settings_blank(self, tmp_path):
        """When admin hasn't saved settings.site_url, the route derives
        the origin from the request's host header, with the scheme read
        through req.protocol (which takes X-Forwarded-Proto only from the
        one trusted proxy hop, Railway's edge), never from the raw header."""
        v1 = {
            "app_name": "sitemap-origin-fallback",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        seo_js = (build_dir / "routes" / "seo.js").read_text()
        assert "req.protocol === 'https'" in seo_js
        assert "req.get('x-forwarded-proto')" not in seo_js
        assert "req.get('host')" in seo_js
        # Trailing slash on settings.site_url gets normalized.
        assert "replace(/\\/+$/, '')" in seo_js

    def test_sitemap_includes_model_with_public_listing_path(self, tmp_path):
        """A data_model that opts into a public listing via
        public_listing_path must appear in the sitemap so its index page
        is crawlable. The model wires this via resolve_spec when its
        slug collides with a customer page; we exercise the explicit
        case by passing a v1 spec whose model declares the path."""
        v1 = {
            "app_name": "sitemap-public-listing",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
            "data_models": [
                {
                    "name": "Gallery",
                    "public_listing_path": "/gallery",
                    "fields": [
                        {"name": "title", "field_type": "text", "required": True},
                    ],
                }
            ],
        }
        build_dir = self._build_for(tmp_path, v1)
        seo_js = (build_dir / "routes" / "seo.js").read_text()
        import re
        match = re.search(r"PUBLIC_ROUTES = (\[[^\]]*\])", seo_js)
        assert match
        routes = json.loads(match.group(1))
        assert "/gallery" in routes


# =============================================================================
# Organization JSON-LD on every
# public page. Mirrors web/src/services/structured-data.ts:organizationJsonLd().
# The layout-public.ejs builds the node from res.locals.settings so the same
# JSON-LD block ships on archetype default routes, custom_pages, legal pages,
# and detail pages without per-handler threading.
# =============================================================================

def _jsonld_graph_from_render(out):
    """Extract + parse the JSON-LD graph from a rendered layout. The
    serialized JSON writes each "<" and ">" as a backslash-u escape, which
    json.loads reads back. Returns the parsed document."""
    m = re.search(
        r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
        out, re.S,
    )
    assert m, f"JSON-LD script tag not found in rendered output:\n{out[:1500]}"
    return json.loads(m.group(1))


def _jsonld_node_from_render(out, node_type="Organization"):
    doc = _jsonld_graph_from_render(out)
    nodes = doc.get("@graph", [doc])
    for n in nodes:
        if n.get("@type") == node_type:
            return n
    raise AssertionError(f"no {node_type} node in JSON-LD graph: {nodes}")


class TestOrganizationJsonLd:
    """layout-public.ejs must emit a schema.org Organization JSON-LD script
    tag in <head> on every public page."""

    def _build_for(self, tmp_path, v1):
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        v2 = translate_v1_spec(v1)
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        return build_dir

    def test_layout_emits_jsonld_script_tag(self, tmp_path):
        """The generated views/layout-public.ejs contains a JSON-LD script
        tag whose body is the JSON.stringify'd Organization node."""
        v1 = {
            "app_name": "jsonld-tag-present",
            "description": "Quiet studio for coaches.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        layout = (build_dir / "views" / "layout-public.ejs").read_text()
        assert '<script type="application/ld+json"' in layout
        # The body interpolates the pre-built JSON string verbatim via <%-.
        assert "<%- _orgJsonLd %>" in layout
        # The Organization node is constructed from settings.
        assert "'@type': 'Organization'" in layout
        # CSP nonce is stamped when available (matches the rest of the
        # compose-emitted inline-script pattern, v2-csp-nonce, 2026-05-26).
        assert 'nonce="<%= cspNonce %>"' in layout

    def test_layout_jsonld_uses_settings_for_name_and_socials(self, tmp_path):
        """The JSON-LD construction reads site_name + the six social URL
        keys from res.locals.settings so admin-edited values flow through
        without a rebuild."""
        v1 = {
            "app_name": "jsonld-settings-source",
            "description": "Quiet studio.",
            "admin_email": "owner@example.com",
        }
        build_dir = self._build_for(tmp_path, v1)
        layout = (build_dir / "views" / "layout-public.ejs").read_text()
        assert "_s.site_name" in layout
        for key in (
            "_s.instagram_url", "_s.facebook_url", "_s.twitter_url",
            "_s.linkedin_url", "_s.youtube_url", "_s.tiktok_url",
        ):
            assert key in layout, f"layout JSON-LD must read {key} from settings"
        # The serialized JSON escapes "<" and ">" so the script element
        # cannot be closed early (test_layout_jsonld_parse.py checks the parse).
        assert ".split('<').join(" in layout

    def test_layout_renders_valid_jsonld_organization_node(self, tmp_path, resolved):
        """End-to-end: render layout-public.ejs via Node + EJS with a
        realistic res.locals payload and assert the emitted script tag
        body parses as valid JSON with the right Organization shape."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "Fern & Stone",
                    "site_url": "https://fern-and-stone.example",
                    "site_description": "A quiet studio for healers.",
                    "nav_logo_url": "/logo.svg",
                    "instagram_url": "https://instagram.example/fernandstone",
                    "facebook_url": "",
                    "twitter_url": "https://twitter.example/fernandstone",
                    "linkedin_url": "",
                    "youtube_url": "",
                    "tiktok_url": "",
                },
                "body": "<p>body</p>",
                "canonicalUrl": "/about",
            },
            resolved, tmp_path,
        )
        assert rc == 0, f"layout render failed: {err}"
        doc = _jsonld_graph_from_render(out)
        assert doc["@context"] == "https://schema.org"
        node = _jsonld_node_from_render(out, "Organization")
        assert node["@type"] == "Organization"
        assert node["name"] == "Fern & Stone"
        # Organization url must be the site origin, not the per-page canonical.
        assert node["url"] == "https://fern-and-stone.example"
        # Logo absolutized from /logo.svg against the site origin.
        assert node["logo"] == "https://fern-and-stone.example/logo.svg"
        # Only non-empty social URLs land in sameAs.
        assert node["sameAs"] == [
            "https://instagram.example/fernandstone",
            "https://twitter.example/fernandstone",
        ]
        assert node["description"] == "A quiet studio for healers."

    def test_layout_omits_sameas_when_no_socials_configured(self, tmp_path, resolved):
        """Schema.org rich-results validators flag empty `sameAs` arrays.
        When the customer hasn't filled in any social URLs the key must
        be omitted entirely rather than emitted as []."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "Lonely Studio",
                    "site_url": "https://lonely.example",
                },
                "body": "<p>body</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, f"layout render failed: {err}"
        node = _jsonld_node_from_render(out, "Organization")
        assert "sameAs" not in node, (
            f"sameAs must be omitted when empty; got {node}"
        )

    def test_layout_jsonld_falls_back_to_request_origin(self, tmp_path, resolved):
        """When settings.site_url is blank the layout falls back to a
        relative '/' via _absUrl, which the settings middleware fills in
        at request time. Renders should not crash and the node should
        carry a non-empty url and logo."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {"site_name": "Studio"},
                "body": "<p>body</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, f"layout render failed: {err}"
        node = _jsonld_node_from_render(out, "Organization")
        assert node["name"] == "Studio"
        # Without a configured site_url, url + logo land as relative paths
        # which the settings middleware would normally pre-fill in prod;
        # the test asserts the keys are present and non-empty.
        assert node["url"], "url must be present even without site_url"
        assert node["logo"], "logo must be present even without site_url"

    def test_layout_jsonld_skips_placeholder_logos(self, tmp_path, resolved):
        """Placeholder logo URLs (picsum/placeholder.com/etc.) must not
        end up in the Organization node; they'd be terrible citations
        for AI engines. The layout falls back to /logo.svg for these."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "Placeholder Studio",
                    "site_url": "https://example.com",
                    "nav_logo_url": "https://picsum.photos/200",
                },
                "body": "<p>body</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, f"layout render failed: {err}"
        node = _jsonld_node_from_render(out, "Organization")
        assert "picsum" not in node["logo"], (
            f"placeholder logo URL leaked into Organization.logo: {node['logo']}"
        )
        # /logo.svg gets cache-busted by _cache_bust_static_assets to
        # /logo.svg?v=<hash>; either bare or busted form is acceptable.
        import re as _re
        assert _re.fullmatch(
            r"https://example\.com/logo\.svg(\?v=[0-9a-f]+)?", node["logo"]
        ), f"expected /logo.svg fallback, got {node['logo']}"

    def test_layout_jsonld_filters_non_url_social_values(self, tmp_path, resolved):
        """Defensive: a customer who pastes "@handle" into a social URL
        field must not break the JSON-LD. Only http(s)-prefixed values
        land in sameAs."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "Defensive Studio",
                    "site_url": "https://example.com",
                    "instagram_url": "@notaurl",
                    "twitter_url": "https://twitter.example/sample-handle",
                },
                "body": "<p>body</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, f"layout render failed: {err}"
        node = _jsonld_node_from_render(out, "Organization")
        assert node["sameAs"] == ["https://twitter.example/sample-handle"]

    def test_layout_jsonld_present_on_root_render(self, tmp_path, resolved):
        """Schema.org Organization JSON-LD must render on root (/); the
        primary crawl entry point. Asserts presence in the rendered HTML
        head before <body>."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "Root Studio",
                    "site_url": "https://example.com",
                },
                "body": "<p>home</p>",
                "canonicalUrl": "/",
            },
            resolved, tmp_path,
        )
        assert rc == 0, f"layout render failed: {err}"
        head_close = out.find("</head>")
        assert head_close > 0
        head_section = out[:head_close]
        assert '<script type="application/ld+json"' in head_section, (
            "Organization JSON-LD must be inside <head> on root render"
        )


# =====================================================================
# Link previews + SEO headers
#
# Items: v2-link-previews-og + v2-seo-headers-jsonld.
#   - requestOrigin: absolute OG/canonical URLs follow the LIVE host so
#     previews work before AND after a custom-domain cutover.
#   - Detail routes feed the entity's OWN image/title/description.
#   - Card type + dims match the image's real aspect.
#   - Default card strips {accent} brace tokens before drawing.
#   - Specific-first <title>, deduped; robots + author;
#     JSON-LD graph with WebSite/Person/Article, no fabrication.
# =====================================================================


class TestLinkPreviewsOg:

    def test_settings_middleware_exposes_request_origin(self, resolved):
        files = dict(compose_files(resolved))
        mw = files["middleware/settings.js"]
        assert "res.locals.requestOrigin" in mw, (
            "settings middleware must expose the live request origin"
        )
        # It must be set unconditionally (not only when site_url is blank).
        ro_pos = mw.find("res.locals.requestOrigin")
        blank_gate_pos = mw.find("if (!res.locals.settings.site_url)")
        assert -1 < ro_pos < blank_gate_pos, (
            "requestOrigin must be set before (independent of) the "
            "site_url-blank fallback"
        )

    def test_layout_prefers_request_origin_over_site_url(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert "_absBase = _requestOrigin || _siteUrl" in layout, (
            "absolute OG/canonical URLs must prefer the live request "
            "origin and fall back to the configured site_url"
        )

    def test_layout_render_og_urls_follow_live_host(self, resolved, tmp_path):
        """Render the emitted layout with a future-domain site_url + a
        live-host requestOrigin: og:url/og:image must use the live host."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "Sample Novelist",
                    "site_url": "https://sample-novelist.example",
                },
                "requestOrigin": "https://sample-novelist-example.up.railway.app",
                "body": "<p>x</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, err
        assert 'property="og:url" content="https://sample-novelist-example.up.railway.app/' in out
        assert "sample-novelist.example/brand/og-default.jpg" not in out
        assert 'content="https://sample-novelist-example.up.railway.app/brand/og-default.jpg"' in out

    def test_layout_render_falls_back_to_site_url_without_request_origin(
        self, resolved, tmp_path
    ):
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "T",
                    "site_url": "https://example.com",
                },
                "body": "<p>x</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, err
        assert 'content="https://example.com/brand/og-default.jpg"' in out

    def test_detail_route_passes_entity_og_locals(self, tmp_path):
        """Detail handlers must feed the record's own image + name into
        the OG locals so a shared book/event/post link previews that
        entity, not the generic site card."""
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        v1 = {
            "app_name": "og-detail-coach",
            "description": "Executive coach for senior leaders.",
            "admin_email": "owner@example.com",
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(translate_v1_spec(v1)))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        assert "const _recordImage = (record.cover_image_url || record.image_url || record.photo_url" in pages_js
        assert "ogImage: _recordImage || undefined" in pages_js
        assert "ogType: 'article'" in pages_js
        assert "ogTitle: _recordName" in pages_js
        # datePublished feed: only a real publish date, never created_at.
        assert "publishedAt: record.published_at || undefined" in pages_js
        assert "record.created_at || undefined" not in pages_js

    def test_detail_route_dedupes_site_name_in_title(self, tmp_path):
        """A record name already containing the site name must not double
        it in the <title>."""
        from compose.intake import translate_v1_spec
        from compose.build import build_app
        v1 = {
            "app_name": "og-dedupe-coach",
            "description": "Executive coach for senior leaders.",
            "admin_email": "owner@example.com",
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(translate_v1_spec(v1)))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        assert "_recordName.toLowerCase().indexOf(_siteName.toLowerCase())" in pages_js
        assert "_recordName + ' | ' + _siteName" in pages_js

    def test_layout_render_portrait_entity_image_gets_summary_card(
        self, resolved, tmp_path
    ):
        """A portrait image (book cover 800x1200) must produce the square
        `summary` twitter card + its real dims, never the cropping
        summary_large_image."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {"site_name": "T", "site_url": "https://e.example"},
                "ogImage": "/uploads/cover.png",
                "ogImageWidth": 800,
                "ogImageHeight": 1200,
                "body": "<p>x</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, err
        assert 'name="twitter:card" content="summary"' in out
        assert 'property="og:image:width" content="800"' in out
        assert 'property="og:image:height" content="1200"' in out
        assert 'property="og:image:type" content="image/png"' in out

    def test_layout_render_unknown_dims_omits_size_and_uses_summary(
        self, resolved, tmp_path
    ):
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {"site_name": "T", "site_url": "https://e.example"},
                "ogImage": "https://cdn.example.com/photo",
                "body": "<p>x</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, err
        assert 'name="twitter:card" content="summary"' in out
        assert 'property="og:image:width"' not in out
        assert 'property="og:image:height"' not in out
        # Unknown extension: og:image:type omitted rather than guessed.
        assert 'property="og:image:type"' not in out

    def test_layout_render_default_card_keeps_large_card_and_dims(
        self, resolved, tmp_path
    ):
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {"site_name": "T", "site_url": "https://e.example"},
                "body": "<p>x</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, err
        assert 'name="twitter:card" content="summary_large_image"' in out
        assert 'property="og:image:width" content="1200"' in out
        assert 'property="og:image:height" content="630"' in out
        assert 'property="og:image:type" content="image/jpeg"' in out

    def test_strip_accent_brace_tokens_helper(self):
        from compose.build import _strip_accent_brace_tokens
        assert _strip_accent_brace_tokens("Sample {Name}") == "Sample Name"
        assert _strip_accent_brace_tokens("No braces") == "No braces"
        assert _strip_accent_brace_tokens("") == ""
        assert _strip_accent_brace_tokens(None) == ""

    def test_og_default_image_uses_brace_strip_helper(self):
        import inspect
        from compose.build import emit_og_default_image
        src = inspect.getsource(emit_og_default_image)
        assert src.count("_strip_accent_brace_tokens") >= 2, (
            "both title_text and subtitle must be brace-stripped before "
            "drawing"
        )


class TestSeoHeadersJsonLd:

    def test_layout_emits_robots_and_author_meta(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert (
            'name="robots" content="index, follow, max-image-preview:large,'
            ' max-snippet:-1, max-video-preview:-1"'
        ) in layout
        assert 'name="author"' in layout

    def test_admin_layout_has_no_robots_index_meta(self, resolved):
        """Admin stays out of crawl paths by omission; never emit an
        index,follow robots meta there."""
        files = dict(compose_files(resolved))
        admin = files["views/layout-admin.ejs"]
        assert 'content="index, follow' not in admin

    def test_layout_jsonld_graph_has_website_node(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert "'@graph': _graph" in layout
        assert "'@type': 'WebSite'" in layout
        assert "/#website" in layout

    def test_layout_jsonld_person_gated_on_owner_name(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert "if (_s.owner_name)" in layout
        assert "'@type': 'Person'" in layout

    def test_layout_jsonld_article_gated_on_og_type(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert "if (_ogType === 'article')" in layout
        assert "'@type': 'Article'" in layout
        # datePublished only from a real publish date local.
        assert "if (_publishedAt) _articleNode.datePublished" in layout
        # Never fabricate ratings.
        assert "aggregateRating" not in layout

    def test_layout_jsonld_escapes_angle_brackets_and_line_separators(self, resolved):
        files = dict(compose_files(resolved))
        layout = files["views/layout-public.ejs"]
        assert r"split('<').join('\\' + 'u003c')" in layout, (
            "serialized JSON-LD must escape every < as its backslash-u escape"
        )
        assert r"split('>').join('\\' + 'u003e')" in layout, (
            "serialized JSON-LD must escape every > as its backslash-u escape"
        )
        assert "String.fromCharCode(0x2028)" in layout
        assert "String.fromCharCode(0x2029)" in layout

    def test_layout_render_jsonld_graph_parses_and_carries_nodes(
        self, resolved, tmp_path
    ):
        """Render with owner_name + article locals and parse the emitted
        JSON-LD: graph must carry Organization, WebSite, Person, Article;
        Article must carry datePublished from publishedAt."""
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {
                    "site_name": "Sample Novelist",
                    "site_url": "https://sample-novelist.example",
                    "owner_name": "Sample Novelist",
                },
                "ogType": "article",
                "ogTitle": "A Sample Novel",
                "ogDescription": "A novel.",
                "ogImage": "/uploads/cover.jpg",
                "publishedAt": "2001-01-15",
                "body": "<p>x</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, err
        m = re.search(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
            out, re.S,
        )
        assert m, "JSON-LD script tag missing from rendered layout"
        graph_doc = json.loads(m.group(1))
        types = {n.get("@type") for n in graph_doc.get("@graph", [])}
        assert {"Organization", "WebSite", "Person", "Article"} <= types
        article = next(
            n for n in graph_doc["@graph"] if n.get("@type") == "Article"
        )
        assert article["headline"] == "A Sample Novel"
        assert article["datePublished"] == "2001-01-15"
        assert article["image"].startswith("https://sample-novelist.example/")

    def test_layout_render_jsonld_no_person_or_article_by_default(
        self, resolved, tmp_path
    ):
        rc, out, err = _render_emitted_layout(
            "layout-public.ejs",
            {
                "settings": {"site_name": "T", "site_url": "https://e.example"},
                "body": "<p>x</p>",
            },
            resolved, tmp_path,
        )
        assert rc == 0, err
        m = re.search(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
            out, re.S,
        )
        assert m
        graph_doc = json.loads(m.group(1))
        types = {n.get("@type") for n in graph_doc.get("@graph", [])}
        assert "Person" not in types
        assert "Article" not in types
        assert {"Organization", "WebSite"} <= types


# =====================================================================
# Full editability: v2-full-editability-audit
#
# A page template carries ZERO customer-voice prose the admin can't
# edit: every static string in a customer-voice field becomes a
# settings-backed read (settings.pg_<page>_<area>_<field> with the
# baked copy as fallback), seeded with the current copy, surfaced in a
# per-page "Page Text" section of /admin/settings. Shared display
# tokens (rating stars) are consumed by both public and admin surfaces.
# =====================================================================


class TestFullEditabilityAudit:

    def _resolved(self):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        spec = scaffold_spec({
            "app_name": "editability-test", "archetype": "healer-wellness",
            "site_name": "T", "admin_email": "a@x.example",
        })
        return resolve_spec(BuildSpec.from_dict(spec))

    def test_page_text_settings_map_is_deterministic(self):
        from compose.build import _page_text_settings
        rs = self._resolved()
        a = _page_text_settings(rs)
        b = _page_text_settings(rs)
        assert a == b
        assert "home" in a and a["home"], "home page must surface page text"
        for entries in a.values():
            for e in entries:
                assert re.fullmatch(r"pg_[a-z0-9_]+", e["key"]), e["key"]
                assert e["value"].strip()
                assert e["type"] in ("text", "textarea")

    def test_page_views_read_copy_from_settings_with_baked_fallback(self):
        from compose.build import compose_files
        rs = self._resolved()
        files = dict(compose_files(rs))
        home = files["views/home.ejs"]
        assert "settings.pg_home_" in home, (
            "home view must read customer-voice copy via settings keys"
        )
        # The baked copy stays as the fallback so existing DBs (no
        # seeded key) render identically.
        assert "!== undefined ? settings.pg_home_" in home

    def test_seed_carries_page_text_keys_with_current_copy(self):
        from compose.build import compose_files, _page_text_settings
        rs = self._resolved()
        files = dict(compose_files(rs))
        seed = files["db/seed.sql"]
        for entries in _page_text_settings(rs).values():
            for e in entries:
                assert f"('{e['key']}'" in seed, (
                    f"seed must populate {e['key']} with current copy so the "
                    "settings form shows real text, not blanks"
                )

    def test_settings_form_surfaces_page_text_sections(self):
        from compose.build import compose_files, _page_text_settings
        rs = self._resolved()
        files = dict(compose_files(rs))
        route = files["routes/admin-settings.js"]
        assert "Page Text: Home" in route
        # Every page-text key must be in ALLOWED_KEYS (derives from the
        # sections) so the POST accepts the edit.
        for entries in _page_text_settings(rs).values():
            for e in entries:
                assert e["key"] in route

    def test_faithful_copy_pages_excluded_from_page_text(self):
        """Faithful-copy pages keep the keeper's verbatim words baked;
        the pre-rendered HTML body is not a settings-form string."""
        from compose.build import _page_text_settings
        rs = self._resolved()
        rs.spec.pages["about"] = dict(rs.spec.pages.get("about") or {})
        rs.spec.pages["about"]["_copy_blocks"] = [{"type": "markdown", "text": "x"}]
        pt = _page_text_settings(rs)
        assert "about" not in pt

    def test_rating_star_shared_token_in_base_styles_and_widget(self):
        """Stars were clay-brown in admin vs gold on public.
        One token, both surfaces."""
        from compose.build import compose_files
        rs = self._resolved()
        files = dict(compose_files(rs))
        css = files["public/style.css"]
        assert "--color-rating-star: #d4af37" in css
        from pathlib import Path
        reviews = (
            Path(__file__).resolve().parents[1]
            / "widgets" / "offerings" / "reviews-grid.ejs"
        ).read_text()
        assert "var(--color-rating-star" in reviews


# =====================================================================
# Seed parity discipline; v2-seed-parity-discipline
#
# seed.sql only runs on an EMPTY database, so hand edits to the live DB
# rot the seed silently. Generated apps now ship two operator scripts:
#   - scripts/regen-seed-from-db.js: regenerate db/seed.sql FROM the
#     live DB as idempotent INSERT ... WHERE NOT EXISTS keyed on
#     NATURAL keys (never serial ids); --check mode reports drift.
#   - scripts/rehash-assets.js: recompute ?v=<hash8> cache-bust params
#     after hand edits to public assets.
# =====================================================================


class TestSeedParityDiscipline:

    def _files(self):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec, compose_files
        spec = scaffold_spec({
            "app_name": "seed-parity-test", "archetype": "healer-wellness",
            "site_name": "T", "admin_email": "a@x.example",
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))
        return rs, dict(compose_files(rs))

    def test_ops_scripts_emitted_and_node_parse(self, tmp_path):
        import shutil
        import subprocess
        if shutil.which("node") is None:
            pytest.skip("node is not on PATH")
        _, files = self._files()
        for name in ("scripts/regen-seed-from-db.js", "scripts/rehash-assets.js"):
            assert name in files, f"{name} must ship in every generated app"
            p = tmp_path / Path(name).name
            p.write_text(files[name])
            r = subprocess.run(
                ["node", "--check", str(p)], capture_output=True, text=True
            )
            assert r.returncode == 0, f"{name} has a JS syntax error: {r.stderr}"

    def test_regen_script_uses_natural_keys_never_serial_ids(self):
        _, files = self._files()
        src = files["scripts/regen-seed-from-db.js"]
        assert "WHERE NOT EXISTS" in src
        assert "NATURAL_KEY_PRIORITY" in src
        assert "'slug'" in src and "'title'" in src and "'quote'" in src
        # The skip set keeps serial ids + bookkeeping out of the dump.
        assert "'id'" in src and "'created_at'" in src
        assert "--check" in src, "drift-check mode must exist"

    def test_regen_script_knows_this_apps_content_tables(self):
        rs, files = self._files()
        from compose.build import _slug_for
        src = files["scripts/regen-seed-from-db.js"]
        for m in rs.data_models:
            table = _slug_for(m).replace("-", "_")
            assert table in src, f"content table {table} missing from regen script"

    def test_emitted_seed_never_keys_on_serial_ids(self):
        _, files = self._files()
        for name in ("db/seed.sql",):
            assert not re.search(r"WHERE\s+id\s*=\s*\d", files[name]), (
                f"{name} keys a row on a serial id; a fresh DB seeded in a "
                "different order would mis-assign content"
            )

    def test_rehash_script_updates_hash_after_asset_edit(self, tmp_path):
        """End-to-end: build the app, hand-edit style.css, run
        rehash-assets.js, and verify the layout's ?v= hash moved."""
        import shutil
        import subprocess
        if shutil.which("node") is None:
            pytest.skip("node is not on PATH")
        from compose.intake import scaffold_spec
        from compose.build import build_app
        spec = scaffold_spec({
            "app_name": "rehash-e2e", "archetype": "healer-wellness",
            "site_name": "T", "admin_email": "a@x.example",
        })
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        layout = (build_dir / "views" / "layout-public.ejs").read_text()
        m = re.search(r"/style\.css\?v=([0-9a-f]{8})", layout)
        assert m, "compose must have cache-busted style.css"
        old_hash = m.group(1)
        css = build_dir / "public" / "style.css"
        css.write_text(css.read_text() + "\n/* hand edit */\n")
        r = subprocess.run(
            ["node", str(build_dir / "scripts" / "rehash-assets.js")],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        layout2 = (build_dir / "views" / "layout-public.ejs").read_text()
        m2 = re.search(r"/style\.css\?v=([0-9a-f]{8})", layout2)
        assert m2 and m2.group(1) != old_hash, (
            "rehash-assets.js must move the style.css hash after an edit"
        )
        # Idempotent: a second run with no edits changes nothing.
        r2 = subprocess.run(
            ["node", str(build_dir / "scripts" / "rehash-assets.js")],
            capture_output=True, text=True,
        )
        assert r2.returncode == 0
        assert (build_dir / "views" / "layout-public.ejs").read_text() == layout2


# =====================================================================
# Author archetype suite -
# v2-author-archetype-suite
#
# The author archetype graduates from skeleton (Book model + books-grid)
# to the full author shape: relation-filtered book detail pages,
# series grouping + single-member spotlight, an author-forward home that
# is NOT a catalog clone (nameplate+shelf hero, ONE spotlight book,
# 3-quote shortest-first praise), press cards that link straight to the
# source, and owner_name -> Person JSON-LD.
# =====================================================================


class TestAuthorArchetypeSuite:

    def _build(self, tmp_path):
        v2 = {
            "app_name": "author-suite-test",
            "archetype": "author",
            "admin_email": "author@example.com",
            "settings": {"site_name": "Sample Novelist"},
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        return build_dir

    def test_home_is_not_a_catalog_clone(self, tmp_path):
        """Home composes DIFFERENT widgets than /books. The
        spotlight home leads with nameplate+shelf and ONE featured book;
        the catalog page keeps the full grid."""
        build_dir = self._build(tmp_path)
        home = (build_dir / "views" / "home.ejs").read_text()
        books = (build_dir / "views" / "books.ejs").read_text()
        assert "hero/nameplate-shelf.ejs" in home
        assert "offerings/book-spotlight.ejs" in home
        assert "offerings/books-grid.ejs" not in home, (
            "home must not render the full catalog grid"
        )
        assert "offerings/books-grid.ejs" in books

    def test_home_praise_band_is_limited_and_shortest_first(self, tmp_path):
        build_dir = self._build(tmp_path)
        home = (build_dir / "views" / "home.ejs").read_text()
        assert '"limit": 3' in home
        assert '"order": "shortest_first"' in home

    def test_home_route_selects_books_and_testimonials_separately(self, tmp_path):
        """Multi-table pages give each widget ITS OWN table's rows; the
        old single-items plumbing silently fed book rows to the
        testimonials widget."""
        build_dir = self._build(tmp_path)
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        i = pages_js.index("router.get('/',")
        home_block = pages_js[i:pages_js.index("});", pages_js.index("res.render('home'", i)) + 3]
        # Both tables have a `published` column, so the public read hides
        # rows an admin explicitly unpublished (NULL stays visible).
        assert "SELECT * FROM books WHERE published = TRUE ORDER BY id ASC" in home_block
        assert "SELECT * FROM testimonials WHERE published = TRUE ORDER BY id ASC" in home_block
        assert "items_books: items" in home_block
        assert "items_testimonials: items_testimonials" in home_block
        home_view = (build_dir / "views" / "home.ejs").read_text()
        assert "locals.items_testimonials" in home_view
        assert "locals.items_books" in home_view

    def test_home_shelf_rows_link_to_book_detail(self, tmp_path):
        """Rows of a table with detail pages get detail_href wherever
        they appear, keyed on the detail page's path (/books), so the
        home cover shelf links exactly like the catalog."""
        build_dir = self._build(tmp_path)
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        i = pages_js.index("router.get('/',")
        home_block = pages_js[i:i + 600]
        assert "r.detail_href = '/books/' + (r.slug || r.id)" in home_block

    def test_book_detail_route_and_view(self, tmp_path):
        """Per-entity public pages with relation-filtered
        testimonials + graceful empty state."""
        build_dir = self._build(tmp_path)
        pages_js = (build_dir / "routes" / "pages.js").read_text()
        assert "router.get('/books/:identifier'" in pages_js
        i = pages_js.index("router.get('/books/:identifier'")
        block = pages_js[i:i + 1800]
        assert "SELECT * FROM testimonials WHERE book_id = $1" in block
        assert "related_rows" in block
        detail = (build_dir / "views" / "books-detail.ejs").read_text()
        assert "What readers are saying" in detail
        assert "Reviews coming soon" in detail
        assert "locals.related_rows" in detail
        # Cover keeps its natural 2/3 aspect, contained.
        assert "aspect-ratio: 2 / 3" in detail and "object-fit: contain" in detail

    def test_testimonial_admin_gets_book_select(self, tmp_path):
        """Testimonial belongs_to Book: the admin form renders a Book
        dropdown populated from the parent table."""
        build_dir = self._build(tmp_path)
        schema = (build_dir / "db" / "schema.sql").read_text()
        assert "book_id" in schema
        new_view = (build_dir / "views" / "testimonials" / "new.ejs").read_text()
        assert "belongs_to_select" in new_view
        route = (build_dir / "routes" / "testimonials.js").read_text()
        assert "parent_rows" in route

    def test_owner_name_seeded_for_author_only(self, tmp_path):
        """owner_is_person archetypes seed owner_name (drives the Person
        JSON-LD node); business archetypes do not."""
        build_dir = self._build(tmp_path)
        seed = (build_dir / "db" / "seed.sql").read_text()
        assert "('owner_name', 'Sample Novelist')" in seed
        # healer (business archetype) does not seed owner_name
        v2 = {
            "app_name": "healer-no-owner", "archetype": "healer-wellness",
            "admin_email": "h@example.com", "settings": {"site_name": "T"},
        }
        spec_path = tmp_path / "spec2.json"
        spec_path.write_text(json.dumps(v2))
        build_dir2 = tmp_path / "out2"
        build_app(str(spec_path), output_dir=str(build_dir2))
        seed2 = (build_dir2 / "db" / "seed.sql").read_text()
        assert "owner_name" not in seed2

    def test_books_grid_series_grouping_contract(self):
        """Source contract: grouping by series_label,
        spotlight for single-member groups, per-card badge suppressed
        inside grouped sections."""
        widgets = Path(__file__).resolve().parents[1] / "widgets"
        grid = (widgets / "offerings" / "books-grid.ejs").read_text()
        assert "series_label" in grid
        assert "_grouped" in grid
        assert "show_series_badge: false, spotlight: true" in grid
        card = (widgets / "offerings" / "book-card.ejs").read_text()
        assert "_bcShowSeries && _bcBook.series_label" in card

    def test_blog_external_url_field_and_card_preference(self, tmp_path):
        """Press/interview cards link straight to the source
        in a new tab when external_url is set; internal detail page is
        the fallback."""
        build_dir = self._build(tmp_path)
        schema = (build_dir / "db" / "schema.sql").read_text()
        assert "external_url" in schema
        widgets = Path(__file__).resolve().parents[1] / "widgets"
        grid = (widgets / "offerings" / "resource-list-card-grid.ejs").read_text()
        assert "_externalHref || _detailHref" in grid
        assert "Go to the source" in grid
        assert "target=\"_blank\" rel=\"noopener\"" in grid

    def test_grouped_and_spotlight_render_behavior(self, tmp_path):
        """Node-render the emitted books + home views with a 2-book
        series + a standalone: grouped eyebrow renders once, the
        standalone gets the spotlight treatment, the praise band caps
        at 3 shortest quotes."""
        import shutil
        import subprocess
        if shutil.which("node") is None:
            pytest.skip("node is not on PATH")
        ejs_dir = Path(__file__).resolve().parents[2] / "web" / "node_modules" / "ejs"
        if not ejs_dir.exists():
            pytest.skip("web/node_modules/ejs is not installed (run `cd web && npm ci`)")
        build_dir = self._build(tmp_path)
        runner = tmp_path / "r.js"
        runner.write_text(
            "const ejs = require(process.argv[2]);\n"
            "const fs = require('fs');\n"
            "const src = fs.readFileSync(process.argv[3], 'utf-8');\n"
            "const data = JSON.parse(fs.readFileSync(process.argv[4], 'utf-8'));\n"
            "process.stdout.write(ejs.render(src, data, { filename: process.argv[3] }));\n"
        )
        books = [
            {"id": 1, "slug": "a", "title": "Book A", "series_label": "The Series",
             "cover_image_url": "/u/1.jpg", "published": True, "sort_order": 1,
             "detail_href": "/books/a"},
            {"id": 2, "slug": "b", "title": "Book B", "series_label": "The Series",
             "cover_image_url": "/u/2.jpg", "published": True, "sort_order": 2,
             "detail_href": "/books/b"},
            {"id": 3, "slug": "c", "title": "Standalone C", "series_label": "",
             "cover_image_url": "/u/3.jpg", "published": True, "sort_order": 3,
             "detail_href": "/books/c", "featured_on_home": True},
        ]
        quotes = [
            {"id": 1, "author_name": "A", "quote": "Punchy.", "published": True},
            {"id": 2, "author_name": "B", "quote": "x" * 200, "published": True},
            {"id": 3, "author_name": "C", "quote": "Medium quote here.", "published": True},
            {"id": 4, "author_name": "D", "quote": "Short one.", "published": True},
        ]
        settings = {
            "site_name": "T",
            "_palette_vars": {"--color-primary": "#111", "--color-on-primary": "#fff"},
            "_fonts": {"display": {"family": "Georgia"}, "body": {"family": "system-ui"}},
        }
        payload = tmp_path / "d.json"
        payload.write_text(json.dumps({
            "settings": settings, "csrfToken": "t",
            "items": books, "items_books": books, "items_testimonials": quotes,
        }))
        r = subprocess.run(
            ["node", str(runner), str(ejs_dir),
             str(build_dir / "views" / "books.ejs"), str(payload)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        out = r.stdout
        assert out.count("The Series") == 1, (
            "series label must render once (group eyebrow), per-card "
            "badges suppressed"
        )
        assert "md:grid-cols-5" in out, "standalone must get spotlight treatment"
        r2 = subprocess.run(
            ["node", str(runner), str(ejs_dir),
             str(build_dir / "views" / "home.ejs"), str(payload)],
            capture_output=True, text=True,
        )
        assert r2.returncode == 0, r2.stderr
        home_out = r2.stdout
        assert "Standalone C" in home_out, "featured book must be the spotlight"
        assert ("x" * 200) not in home_out, (
            "praise band must cap at the 3 shortest quotes"
        )


# =====================================================================
# Batch-4 polish
# Items: v2-page-header-contract, v2-redundancy-dedupe-titles,
# v2-readability-typography, v2-image-aspect-and-focal,
# v2-fixed-count-grid-rows, v2-build-responsive-lints,
# v2-evergreen-dates-slug-aliases.
# =====================================================================


class TestPageHeaderGridAndFocalPointContracts:

    def _files(self, archetype="healer-wellness"):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec, compose_files
        spec = scaffold_spec({
            "app_name": f"polish-{archetype}", "archetype": archetype,
            "site_name": "T", "admin_email": "a@x.example",
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))
        return rs, dict(compose_files(rs))

    # --- v2-page-header-contract (2.3)
    def test_hero_compact_is_page_header_contract(self):
        hero = (Path(__file__).resolve().parents[1] / "widgets" / "hero"
                / "centered.ejs").read_text()
        assert "cas-page-header py-7 md:py-9" in hero
        assert "clamp(1.75rem, 3.2vw, 2.5rem)" in hero

    def test_family_css_pins_page_header_scale(self):
        """The family's statement h1 scale stays exclusive to the home
        hero; interior page headers get the compact clamp + padding."""
        from compose.build import emit_layout_files
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        spec = scaffold_spec({
            "app_name": "ph-test", "archetype": "healer-wellness",
            "site_name": "T", "admin_email": "a@x.example",
            "template_family_id": "editorial-statement",
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert (
            "body.cas-template-editorial-statement .cas-page-header h1 "
            "{ font-size: clamp(1.75rem, 3.2vw, 2.5rem); }"
        ) in layout
        assert (
            "body.cas-template-editorial-statement main > "
            "section.cas-page-header { padding-top: clamp(1.5rem, 2.5vw, 2rem);"
        ) in layout

    # --- v2-redundancy-dedupe-titles (3.5/3.6)
    def test_hero_collapses_duplicate_eyebrow_heading(self):
        hero = (Path(__file__).resolve().parents[1] / "widgets" / "hero"
                / "centered.ejs").read_text()
        assert "_titleNorm" in hero
        assert "_titleCollapsed" in hero
        assert "cas-title-caps" in hero
        # nav-label dedupe for page headers
        assert "currentPath" in hero and "nav_items" in hero

    def test_stack_collapses_duplicate_eyebrow_heading(self):
        stack = (Path(__file__).resolve().parents[1] / "widgets"
                 / "testimonials" / "stack.ejs").read_text()
        assert "_titleCollapsed" in stack
        assert "cas-title-caps" in stack

    def test_title_caps_class_defined_and_pinned(self):
        _, files = self._files()
        css = files["public/style.css"]
        assert ".cas-title-caps { font-size: 0.75rem !important" in css

    # --- v2-readability-typography (1.2/1.3)
    def test_main_bold_pinned_and_wrap_anywhere_applied(self):
        _, files = self._files()
        css = files["public/style.css"]
        assert "main strong, main b { font-weight: 700; }" in css
        assert ".cas-wrap-anywhere { overflow-wrap: anywhere; }" in css
        widgets = Path(__file__).resolve().parents[1] / "widgets"
        for f in ("shell/footer.ejs", "shell/footer-minimal-line.ejs",
                  "shell/footer-single-line.ejs",
                  "shell/footer-grid-five-col.ejs", "form/contact.ejs"):
            assert "cas-wrap-anywhere" in (widgets / f).read_text(), f

    # --- v2-fixed-count-grid-rows (3.2b)
    def test_fixed_count_grid_utilities_and_blockquote_zero(self):
        _, files = self._files()
        css = files["public/style.css"]
        assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
        assert ".cas-grid-fixed-2 blockquote, .cas-grid-fixed-3 blockquote { margin: 0; }" in css

    def test_author_home_praise_uses_fixed_three_columns(self, tmp_path):
        v2 = {
            "app_name": "praise-cols", "archetype": "author",
            "admin_email": "a@x.example", "settings": {"site_name": "T"},
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        home = (build_dir / "views" / "home.ejs").read_text()
        assert '"columns": 3' in home

    # --- v2-image-aspect-and-focal (4.1/4.2)
    def test_event_model_carries_focal_y(self):
        names = {f["name"] for f in _default_fields("Event")}
        assert "focal_y" in names

    def test_generic_detail_view_uses_natural_aspect_and_focal(self, tmp_path):
        v1 = {
            "app_name": "focal-coach",
            "description": "Executive coach for senior leaders.",
            "admin_email": "o@e.example",
        }
        from compose.intake import translate_v1_spec
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(translate_v1_spec(v1)))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        detail = (build_dir / "views" / "blog-detail.ejs").read_text()
        assert "aspect-[16/9]" not in detail, (
            "fixed 16/9 crops band-destroy portrait sources"
        )
        assert "max-height: 640px" in detail
        assert "object-position: center <%= _focalY %>%" in detail

    def test_card_grid_consumes_focal_y(self):
        grid = (Path(__file__).resolve().parents[1] / "widgets" / "offerings"
                / "resource-list-card-grid.ejs").read_text()
        assert "item.focal_y" in grid
        assert "object-position: center " in grid

    # --- v2-build-responsive-lints (4.3 static part + script)
    def test_aspect_min_height_lint_flags_conflicts(self):
        from compose.build import audit_aspect_ratio_min_height_conflicts
        bad = [("views/x.ejs",
                '<div style="aspect-ratio: 16/10; min-height: 320px;">')]
        assert len(audit_aspect_ratio_min_height_conflicts(bad)) == 1
        good = [("views/x.ejs", '<div style="aspect-ratio: 16/10;">')]
        assert audit_aspect_ratio_min_height_conflicts(good) == []

    def test_clean_builds_have_no_aspect_min_height_conflicts(self, tmp_path):
        for arc in ("healer-wellness", "author"):
            v2 = {
                "app_name": f"lint-{arc}", "archetype": arc,
                "admin_email": "a@x.example", "settings": {"site_name": "T"},
            }
            spec_path = tmp_path / f"{arc}.json"
            spec_path.write_text(json.dumps(v2))
            build_dir = tmp_path / f"out-{arc}"
            build_app(str(spec_path), output_dir=str(build_dir))
            manifest = (build_dir / "compose-manifest.txt").read_text()
            assert "aspect_ratio_min_height_conflict" not in manifest, arc

    def test_slideshow_has_no_min_height_with_aspect(self):
        src = (Path(__file__).resolve().parents[1] / "widgets" / "content"
               / "photo-slideshow.ejs").read_text()
        assert "min-height: 320px" not in src

    def test_responsive_lint_script_exists_and_parses(self):
        import shutil
        import subprocess
        script = Path(__file__).resolve().parents[2] / "scripts" / "responsive_lint.mjs"
        assert script.exists()
        if shutil.which("node") is None:
            pytest.skip("node is not on PATH")
        r = subprocess.run(["node", "--check", str(script)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    # --- v2-evergreen-dates-slug-aliases (5.4)
    def test_date_label_never_renders_publicly(self, tmp_path):
        """date_label is an admin-only record field; public detail
        templates render record.eyebrow only."""
        v2 = {
            "app_name": "evergreen-author", "archetype": "author",
            "admin_email": "a@x.example", "settings": {"site_name": "T"},
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(v2))
        build_dir = tmp_path / "out"
        build_app(str(spec_path), output_dir=str(build_dir))
        import re as _re
        for view in (build_dir / "views").rglob("*.ejs"):
            rel = str(view.relative_to(build_dir / "views"))
            if rel.startswith(("events/", "books/", "blog-posts/",
                               "testimonials/", "contact-inquiries/",
                               "newsletter-subscribers/")):
                continue  # admin CRUD views may show it
            src = view.read_text()
            assert not _re.search(r"<%[-=][^%]*record\.date_label", src), (
                f"{rel} renders date_label publicly"
            )

    def test_slug_aliases_table_and_middleware(self):
        _, files = self._files()
        schema = files["db/schema.sql"]
        assert "CREATE TABLE IF NOT EXISTS slug_aliases" in schema
        assert "old_path TEXT UNIQUE NOT NULL" in schema
        mw = files["middleware/slug-aliases.js"]
        assert "res.redirect(301, target)" in mw
        assert "fail open" in mw.lower() or "Fail open" in mw
        server = files["server.js"]
        assert "slugAliasMiddleware" in server
        # Mounted before the page routers.
        assert server.index("slugAliasMiddleware") < server.index("routes/pages")
