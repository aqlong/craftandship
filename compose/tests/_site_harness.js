// Loads built sites and exercises every emitted file, without opening a
// socket. By default also without a database.
//
//   node _site_harness.js <plan.json>
//
// plan = { out: <result path>, apps: [{ id, dir, tables, only, extra_paths, database_url }] }
//   dir     a build directory (what compose.build.build_app wrote)
//   tables  { <table>: [{ name, kind }] }  the columns of each model table,
//           used to answer SELECTs with one plausible row per table
//   only    optional list of relative paths; when given, only those files
//           are parsed (the routes are still all requested)
//   extra_paths
//           optional list of GET paths requested in addition to the ones
//           found in the routers (a row's own URL, which the harness cannot
//           guess); the result's `extra` maps "<visitor|admin> <path>" to
//           the status each was answered with
//   database_url
//           optional. When given, the emitted routers talk to that
//           PostgreSQL database (node-postgres from NODE_PATH) instead of
//           the stand-in, so every statement a GET route runs is run by
//           Postgres. The caller has applied the site's db/schema.sql and
//           db/seed.sql there first; `tables` is not used.
//
// For each app, in this order:
//   1. every .js file is parsed (server files as CommonJS modules, the
//      files under public/ as browser scripts) and every .json file is
//      parsed; every .ejs file under views/ is compiled;
//   2. the emitted routers and middleware are loaded into a real Express
//      app, mounted the way the emitted server.js mounts them, with the
//      real EJS and express-ejs-layouts;
//   3. every GET route is requested twice, as a visitor and as the signed-in
//      admin, and the view it renders is rendered for real.
//
// Stand-ins: db.js (answers from `tables`), the session (a plain object per
// request) and the network (requests are handed to app.handle()). Not run:
// server.js itself (it is parsed), POST handlers, the migration.
//
// A request "fails" when its handler or its view throws, or when it answers
// 500 or above. The result lists each failure with the error text.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const http = require('http');
const net = require('net');
const Module = require('module');

const plan = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const express = require('express');
const ejs = require('ejs');
const expressLayouts = require('express-ejs-layouts');

const realLog = console.log;
if (!process.env.HARNESS_VERBOSE) console.log = console.info = console.debug = console.warn = console.error = function () {};

function walk(dir, out) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

function parseProblems(appDir, only) {
  const problems = [];
  const wanted = only ? new Set(only.map(function (p) { return path.join(appDir, p); })) : null;
  for (const file of walk(appDir, [])) {
    if (wanted && !wanted.has(file)) continue;
    const rel = path.relative(appDir, file);
    try {
      if (file.endsWith('.js')) {
        // node drops a "#!" first line before it parses a file.
        const source = fs.readFileSync(file, 'utf8').replace(/^#![^\n]*/, '');
        if (rel.split(path.sep)[0] === 'public') {
          new vm.Script(source, { filename: file });
        } else {
          // The same wrapper node applies to a CommonJS module, so this
          // is the check `node --check <file>` makes.
          new vm.Script(Module.wrap(source), { filename: file });
        }
      } else if (file.endsWith('.json')) {
        JSON.parse(fs.readFileSync(file, 'utf8'));
      } else if (file.endsWith('.ejs') && rel.split(path.sep)[0] === 'views') {
        ejs.compile(fs.readFileSync(file, 'utf8'), { filename: file });
      }
    } catch (e) {
      problems.push({ file: rel, error: String(e && e.message).split('\n').filter(Boolean).slice(-1)[0] });
    }
  }
  return problems;
}

function sampleValue(column) {
  const name = String(column.name || '').toLowerCase();
  const kind = String(column.kind || '').toLowerCase();
  if (name === 'id') return 1;
  if (/bool/.test(kind)) return true;
  if (/int|serial|numeric|decimal|real|double/.test(kind)) return 3;
  if (/timestamp|date/.test(kind)) return new Date(Date.UTC(2030, 0, 15, 17, 30, 0));
  if (/^time/.test(kind)) return '17:30:00';
  if (/json/.test(kind)) return ['one', 'two'];
  if (/email/.test(name)) return 'visitor@example.com';
  if (/(^|_)(url|href|link|src)$/.test(name)) return 'https://example.com/sample.jpg';
  if (name === 'slug') return 'sample-row';
  return 'Sample ' + name;
}

function makeDb(tables) {
  function rowFor(table) {
    const columns = tables[table];
    const row = { id: 1, created_at: new Date(Date.UTC(2030, 0, 1)) };
    if (!columns) {
      return Object.assign(row, { title: 'Sample', name: 'Sample', slug: 'sample-row', display: 'Sample' });
    }
    for (const column of columns) row[column.name] = sampleValue(column);
    return row;
  }
  const db = {
    calls: [],
    pool: {},
    query: async function (text) {
      const sql = String(text).replace(/\s+/g, ' ').trim();
      db.calls.push(sql);
      if (/^SELECT key, value FROM site_settings/i.test(sql)) return { rows: [], rowCount: 0 };
      if (/^SELECT COUNT/i.test(sql)) return { rows: [{ count: '1', total: '1', n: '1', c: '1' }], rowCount: 1 };
      const from = /\bFROM ([a-z_][a-z0-9_]*)/i.exec(sql);
      const table = from ? from[1].toLowerCase() : '';
      if (/^SELECT/i.test(sql)) {
        if (table === 'slug_aliases' || table === 'magic_link_tokens') return { rows: [], rowCount: 0 };
        if (table === 'users') {
          return { rows: [{ id: 1, email: 'owner@example.com', role: 'admin', password: null, password_set: false }], rowCount: 1 };
        }
        if (table === 'site_settings') return { rows: [], rowCount: 0 };
        const row = rowFor(table);
        // Aggregates and aliases the routers read beside the columns.
        row.count = '1'; row.total = '1'; row.display = row.display || 'Sample';
        return { rows: [row], rowCount: 1 };
      }
      return { rows: [{ id: 1 }], rowCount: 1 };
    },
  };
  return db;
}

// The emitted db.js is {query, pool}; this is the same shape over a real
// connection pool, so a statement Postgres rejects surfaces as the error
// the route's own handler sees.
function makeRealDb(url) {
  const { Pool } = require('pg');
  const pool = new Pool({ connectionString: url, max: 2 });
  const db = {
    calls: [],
    errors: [],
    pool: pool,
    query: function (text, params) {
      db.calls.push(String(text).replace(/\s+/g, ' ').trim());
      return pool.query(text, params).catch(function (e) {
        // A handler that catches this and answers 500 itself would
        // otherwise hide what Postgres said.
        db.errors.push(lastLine(e));
        throw e;
      });
    },
  };
  return db;
}

function collectGetPaths(mount, router, out) {
  for (const layer of (router && router.stack) || []) {
    if (layer.route && layer.route.methods && layer.route.methods.get) {
      const routePaths = Array.isArray(layer.route.path) ? layer.route.path : [layer.route.path];
      for (const routePath of routePaths) {
        if (typeof routePath !== 'string') continue;
        const full = (mount === '/' ? '' : mount) + (routePath === '/' ? '' : routePath);
        out.push(full || '/');
      }
    }
  }
}

function concretePath(routePath) {
  return routePath.replace(/:([A-Za-z_]+)\??/g, function (_m, name) {
    return name === 'id' ? '1' : 'sample-row';
  });
}

function runRequest(app, db, method, url, session) {
  return new Promise(function (resolve) {
    const req = new http.IncomingMessage(new net.Socket());
    req.method = method;
    req.url = url;
    req.headers = { host: 'site.test', accept: 'text/html' };
    req._harnessSession = session;
    const res = new http.ServerResponse(req);
    let settled = false;
    const done = function (result) {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    res._harnessError = null;
    res.end = function (chunk) {
      done({ status: res.statusCode, error: res._harnessError, bytes: chunk ? chunk.length : 0 });
      return res;
    };
    res.write = function () { return true; };
    setTimeout(function () { done({ status: 0, error: 'no answer within 4 seconds', bytes: 0 }); }, 4000);
    try {
      app.handle(req, res);
    } catch (e) {
      done({ status: 0, error: 'threw: ' + String(e && e.message), bytes: 0 });
    }
  });
}

function lastLine(err) {
  const text = String((err && err.message) || err);
  const lines = text.split('\n').filter(function (l) { return l.trim(); });
  return (lines[lines.length - 1] || text).slice(0, 300);
}

async function exercise(spec) {
  const appDir = spec.dir;
  const result = { id: spec.id, parse: [], failures: [], requests: 0, routes: 0, load: null, statuses: {}, extra: {} };
  result.parse = parseProblems(appDir, spec.only || null);

  const dbPath = path.join(appDir, 'db.js');
  const db = spec.database_url ? makeRealDb(spec.database_url) : makeDb(spec.tables || {});
  // The emitted routers require('../db'); answer that from the stand-in.
  require.cache[dbPath] = { id: dbPath, filename: dbPath, loaded: true, exports: db, children: [], paths: [] };

  let app;
  const getPaths = [];
  try {
    const auth = require(path.join(appDir, 'middleware', 'auth.js'));
    const { siteSettingsMiddleware } = require(path.join(appDir, 'middleware', 'settings.js'));
    const { csrfMiddleware } = require(path.join(appDir, 'middleware', 'csrf.js'));
    app = express();
    app.set('trust proxy', 1);
    app.set('views', path.join(appDir, 'views'));
    app.set('view engine', 'ejs');
    app.set('layout', 'layout-public');
    app.use(expressLayouts);
    app.use(function (req, res, next) {
      const session = JSON.parse(JSON.stringify(req._harnessSession || {}));
      session.save = function (cb) { if (cb) cb(); };
      session.regenerate = function (cb) { cb(); };
      session.destroy = function (cb) { if (cb) cb(); };
      req.session = session;
      req.body = {};
      res.locals.cspNonce = 'harness-nonce';
      next();
    });
    app.use(siteSettingsMiddleware);
    app.use(csrfMiddleware);
    app.use(function (req, res, next) {
      res.locals.currentPath = req.path;
      res.locals.user = (req.session && req.session.userId)
        ? { id: req.session.userId, email: req.session.email, role: req.session.role }
        : null;
      next();
    });
    // Same mounts, in the same order, as the emitted server.js.
    const server = fs.readFileSync(path.join(appDir, 'server.js'), 'utf8');
    const mountRe = /^app\.use\('([^']+)',\s*(requireAdmin,\s*)?require\('\.\/routes\/([^']+)'\)\);/gm;
    let found;
    while ((found = mountRe.exec(server)) !== null) {
      const router = require(path.join(appDir, 'routes', found[3] + '.js'));
      if (found[2]) app.use(found[1], auth.requireAdmin, router); else app.use(found[1], router);
      collectGetPaths(found[1], router, getPaths);
    }
    app.use(function (req, res) { res.status(404).render('error-404', { title: '404' }); });
    app.use(function (err, req, res, next) {
      res._harnessError = lastLine(err);
      res.status(500).render('error-500', { title: 'Error' }, function (renderErr, html) {
        if (renderErr) res._harnessError += ' | error-500 view: ' + lastLine(renderErr);
        res.end(html || '');
      });
    });
  } catch (e) {
    // A site whose routers do not load has no routes to ask.
    result.load = lastLine(e);
    app = null;
  }

  if (app) {
    const paths = Array.from(new Set(getPaths.map(concretePath).concat(spec.extra_paths || [])));
    // One path nothing answers, so the 404 view is rendered too.
    paths.push('/no-such-page-in-this-site');
    result.routes = paths.length;
    const sessions = [
      ['visitor', {}],
      ['admin', { userId: 1, email: 'owner@example.com', role: 'admin' }],
    ];
    for (const routePath of paths) {
      for (const pair of sessions) {
        const errorsBefore = db.errors ? db.errors.length : 0;
        const got = await runRequest(app, db, 'GET', routePath, pair[1]);
        if (!got.error && db.errors && db.errors.length > errorsBefore) {
          got.error = 'database: ' + db.errors.slice(errorsBefore).join(' | ');
        }
        result.requests += 1;
        const bucket = pair[0] + ':' + got.status;
        result.statuses[bucket] = (result.statuses[bucket] || 0) + 1;
        if ((spec.extra_paths || []).indexOf(routePath) !== -1) {
          result.extra[pair[0] + ' ' + routePath] = got.status;
        }
        if (got.error || got.status === 0 || got.status >= 500) {
          result.failures.push({ who: pair[0], path: routePath, status: got.status, error: got.error || '' });
        }
      }
    }
  }

  if (spec.database_url && db.pool && typeof db.pool.end === 'function') await db.pool.end();
  for (const key of Object.keys(require.cache)) {
    if (key.startsWith(appDir + path.sep)) delete require.cache[key];
  }
  return result;
}

process.on('unhandledRejection', function (e) {
  realLog(JSON.stringify({ fatal: 'unhandledRejection: ' + lastLine(e) }));
  process.exit(3);
});

(async function () {
  const results = [];
  for (const spec of plan.apps) results.push(await exercise(spec));
  fs.writeFileSync(plan.out, JSON.stringify({ results: results }));
  process.exit(0);
})();
